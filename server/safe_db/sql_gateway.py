"""Guarded, read-only MSSQL Gateway client.

The guard and contract semantics are ported from the locally proven Safe-DB
`scripts/lib/sql_gateway.sh` and `scripts/lib/guard.sh`. Only the SQL Gateway
read path needed by Almighty is included; write, generic DB, and arbitrary-query
interfaces are intentionally absent.
"""

import fcntl
import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

_PARAM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_TARGET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_FORBIDDEN = {
    "alter", "backup", "begin", "bulk", "commit", "create", "dbcc",
    "declare", "delete", "deny", "drop", "exec", "execute", "grant",
    "insert", "kill", "merge", "offset", "opendatasource", "openquery",
    "openrowset", "reconfigure", "restore", "revoke", "rollback", "save",
    "set", "shutdown", "transaction", "truncate", "update", "use", "waitfor",
}


@dataclass(frozen=True)
class GatewayPolicy:
    max_rows: int = 1000
    max_plan_rows: int = 100000
    max_cost: float = 2
    max_execution_ms: int = 5000
    max_response_bytes: int = 5242880
    request_timeout_sec: float = 15

    def request_limits(self) -> dict:
        return {
            "maxRows": self.max_rows,
            "maxPlanRows": self.max_plan_rows,
            "maxCost": self.max_cost,
            "maxExecutionMs": self.max_execution_ms,
            "maxResponseBytes": self.max_response_bytes,
        }


def valid_target_id(value: str) -> bool:
    return bool(isinstance(value, str) and _TARGET_RE.fullmatch(value))


def valid_origin(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        if parsed.scheme == "https" and parsed.hostname:
            return True
        return parsed.scheme == "http" and parsed.hostname in {
            "localhost", "127.0.0.1", "::1",
        }
    except ValueError:
        return False


def _mask_comments_literals_identifiers(sql: str) -> str:
    chars = list(sql)
    i = 0
    while i < len(chars):
        if sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            end = len(chars) if end < 0 else end
            chars[i:end] = " " * (end - i)
            i = end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated block comment")
            end += 2
            chars[i:end] = " " * (end - i)
            i = end
        elif chars[i] in ("'", '"', "["):
            opener = chars[i]
            closer = "]" if opener == "[" else opener
            end = i + 1
            closed = False
            while end < len(chars):
                if chars[end] == closer:
                    if closer != "]" and end + 1 < len(chars) and chars[end + 1] == closer:
                        end += 2
                        continue
                    end += 1
                    closed = True
                    break
                end += 1
            if not closed:
                raise ValueError("unterminated_literal_or_identifier")
            chars[i:end] = " " * (end - i)
            i = end
        else:
            i += 1
    return "".join(chars)


def _outer_select_offset(plain: str) -> int | None:
    depth = 0
    for token in re.finditer(r"[()]|\bselect\b", plain, re.IGNORECASE):
        value = token.group(0).lower()
        if value == "(":
            depth += 1
        elif value == ")":
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0:
            return token.start()
    return None


def guard_read_sql(sql: str, params: dict, max_rows: int) -> None:
    """Port of Safe-DB's SQL Gateway SELECT/TOP/parameter local guards."""
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("empty_sql")
    if not isinstance(params, dict):
        raise ValueError("invalid_params")
    for key, value in params.items():
        if not isinstance(key, str) or not _PARAM_RE.fullmatch(key):
            raise ValueError("invalid_param_name")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("invalid_param_value")

    plain = _mask_comments_literals_identifiers(sql.strip())
    without_params = re.sub(r"@[A-Za-z_][A-Za-z0-9_]*", " ", plain)
    parts = [part.strip() for part in without_params.split(";") if part.strip()]
    if len(parts) != 1:
        raise ValueError("multi_statement")
    head = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)", without_params)
    first = head.group(1).lower() if head else ""
    if first not in {"select", "with"}:
        raise ValueError("select_only")
    if first == "with" and not re.search(r"\bselect\b", without_params, re.IGNORECASE):
        raise ValueError("with_without_select")
    if re.search(r"\blimit\b", without_params, re.IGNORECASE):
        raise ValueError("limit_not_allowed")
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", without_params.lower()):
        if word == "into":
            raise ValueError("select_into")
        if word.startswith("xp_"):
            raise ValueError("extended_procedure")
        if word in _FORBIDDEN:
            raise ValueError(f"forbidden_{word}")
    if "#" in without_params:
        raise ValueError("temporary_table")

    # TOP 위치는 원문과 같은 길이를 보존한 mask에서 찾는다. @param을 제거한 문자열은
    # WITH 절에서 offset을 앞당길 수 있으므로 원문 slicing에 사용하지 않는다.
    outer_offset = _outer_select_offset(plain)
    if outer_offset is None:
        raise ValueError("missing_outer_select")
    outer_plain = plain[outer_offset:]
    scalar = (
        re.match(
            r"^\s*select\s+(?:(?:all|distinct)\s+)?"
            r"(?:count_big|count|sum|avg|min|max)\s*\(",
            outer_plain,
            re.IGNORECASE,
        )
        and not re.search(r"\bgroup\s+by\b", outer_plain, re.IGNORECASE)
        and not re.search(r"\bover\s*\(", outer_plain, re.IGNORECASE)
        and not re.search(r"\b(?:union|intersect|except)\b", outer_plain, re.IGNORECASE)
    )
    outer_sql = sql[outer_offset:]
    top = re.match(
        r"^\s*select\s+(?:(?:all|distinct)\s+)?top\s*\(\s*"
        r"(@[A-Za-z_][A-Za-z0-9_]*|[0-9]+)\s*\)",
        outer_sql,
        re.IGNORECASE,
    )
    if not scalar and top is None:
        raise ValueError("missing_top")
    if top is None:
        return
    if re.match(
        r"^\s*select\s+(?:(?:all|distinct)\s+)?top\s*\([^)]*\)\s*percent\b",
        outer_sql,
        re.IGNORECASE,
    ):
        raise ValueError("top_percent")
    raw = top.group(1)
    if raw.startswith("@"):
        value = params.get(raw[1:])
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("invalid_top_parameter")
    else:
        value = int(raw)
    if value <= 0:
        raise ValueError("invalid_top_limit")
    if value > max_rows:
        raise ValueError("max_rows_exceeded")


def verify_response_contract(data: dict, request_id: str, policy: GatewayPolicy) -> bool:
    if not isinstance(data, dict) or data.get("requestId") != request_id:
        return False
    if not _REQUEST_ID_RE.fullmatch(request_id):
        return False
    applied = data.get("limitsApplied")
    if not isinstance(applied, dict):
        return False
    for key, expected in policy.request_limits().items():
        actual = applied.get(key)
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False
        if isinstance(expected, float):
            if abs(float(actual) - expected) > 1e-9:
                return False
        elif int(actual) != expected:
            return False
    rows = data.get("rows")
    return (
        isinstance(rows, list)
        and len(rows) <= policy.max_rows
        and data.get("rowCount") == len(rows)
    )


@contextmanager
def _connection_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _audit(path: Path, *, sql: str, status: str, detail: str, extra: dict | None = None):
    row = {
        "ts": int(time.time()),
        "action": "read",
        "connection": "mssql-gateway",
        "type": "sql-gateway",
        "sql_hash": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        "status": status,
        "detail": detail,
    }
    if extra:
        row.update({key: value for key, value in extra.items() if value not in (None, "")})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _stream_gateway_json(
    client: httpx.Client, *, url: str, body: dict, headers: dict,
    policy: GatewayPolicy,
) -> dict:
    state: dict = {}
    done = threading.Event()

    def read_response():
        try:
            with client.stream("POST", url, json=body, headers=headers) as response:
                state["response"] = response
                response.raise_for_status()
                raw_length = response.headers.get("content-length")
                if raw_length is not None:
                    try:
                        content_length = int(raw_length)
                    except ValueError as exc:
                        raise ValueError("invalid_content_length") from exc
                    if content_length < 0:
                        raise ValueError("invalid_content_length")
                    if content_length > policy.max_response_bytes:
                        raise ValueError("response_too_large")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    if len(payload) + len(chunk) > policy.max_response_bytes:
                        raise ValueError("response_too_large")
                    payload.extend(chunk)
            state["data"] = json.loads(payload)
        except BaseException as exc:
            state["error"] = exc
        finally:
            done.set()

    reader = threading.Thread(target=read_response, daemon=True)
    reader.start()
    if not done.wait(timeout=policy.request_timeout_sec):
        response = state.get("response")
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass
        raise ValueError("response_deadline_exceeded")
    error = state.get("error")
    if error is not None:
        if isinstance(error, (httpx.HTTPError, ValueError)):
            raise error
        raise ValueError("gateway_stream_failed") from error
    data = state.get("data")
    if not isinstance(data, dict):
        raise ValueError("invalid_response")
    return data


class GuardedSQLGatewayClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        target_field: str,
        lock_path: Path,
        audit_path: Path,
        policy: GatewayPolicy | None = None,
        transport=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.target_field = target_field
        self.lock_path = lock_path
        self.audit_path = audit_path
        self.policy = policy or GatewayPolicy()
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(
            valid_origin(self.base_url)
            and len(self.token) >= 32
            and self.target_field in {"targetId", "hospitalId"}
        )

    def query(self, *, target_id: str, sql: str, params: dict) -> dict | None:
        if not self.configured or not valid_target_id(target_id):
            return None
        try:
            guard_read_sql(sql, params, self.policy.max_rows)
        except ValueError as exc:
            _audit(self.audit_path, sql=sql, status="blocked", detail=str(exc))
            return None
        request_id = secrets.token_hex(16)
        body = {
            self.target_field: target_id,
            "sql": sql,
            "params": params,
            **self.policy.request_limits(),
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Caller-Id": "almighty-pr-review-server",
            "X-Query-Request-Id": request_id,
        }
        with _connection_lock(self.lock_path) as acquired:
            if not acquired:
                _audit(self.audit_path, sql=sql, status="blocked", detail="concurrent_read")
                return None
            try:
                with httpx.Client(
                    timeout=self.policy.request_timeout_sec,
                    transport=self.transport,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    data = _stream_gateway_json(
                        client,
                        url=f"{self.base_url}/query",
                        body=body,
                        headers=headers,
                        policy=self.policy,
                    )
            except (httpx.HTTPError, ValueError):
                self._cancel(request_id, headers)
                _audit(self.audit_path, sql=sql, status="blocked", detail="gateway_failed")
                return None
        if not verify_response_contract(data, request_id, self.policy):
            _audit(self.audit_path, sql=sql, status="blocked", detail="contract_violation")
            return None
        _audit(
            self.audit_path,
            sql=sql,
            status="executed",
            detail="ok",
            extra={
                "target_id": target_id,
                "request_id": request_id,
                "gateway_query_hash": data.get("queryHash"),
                "row_count": data.get("rowCount"),
                "plan_cost": (data.get("plan") or {}).get("cost"),
                "duration_ms": data.get("durationMs"),
            },
        )
        return data

    def _cancel(self, request_id: str, headers: dict) -> None:
        try:
            with httpx.Client(
                timeout=2,
                transport=self.transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                client.post(
                    f"{self.base_url}/query/{request_id}/cancel",
                    headers=headers,
                )
        except httpx.HTTPError:
            pass
