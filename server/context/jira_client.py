import json
import re
from urllib.parse import urlparse

from server import config

_HTTP_TIMEOUT_SEC = 10
_MAX_RESPONSE_BYTES = 1_048_576
_KEY_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")
_AC_FIELD_RE = re.compile(r"customfield_\d+")


class JiraError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


def _validate_base_url(base_url: str) -> str:
    """B-INV-7: fetch-time SSRF guard.
    Jira Cloud의 canonical *.atlassian.net HTTPS origin만 허용해 DNS alias/rebinding과
    userinfo·포트·경로를 통한 대상 우회를 차단한다."""
    try:
        p = urlparse(base_url)
        port = p.port
    except ValueError:
        raise JiraError("jira base_url is invalid") from None
    host = (p.hostname or "").lower()
    if (
        p.scheme != "https"
        or not host.endswith(".atlassian.net")
        or host == "atlassian.net"
        or port not in (None, 443)
        or p.username is not None
        or p.password is not None
        or p.path not in ("", "/")
        or p.query
        or p.fragment
    ):
        raise JiraError("jira base_url host not allowed")
    return base_url.rstrip("/")


class _BufferedResponse:
    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content

    def json(self):
        return json.loads(self.content)


class JiraClient:
    """gh.py 규율을 미러: 주입 http로 테스트 가능, 에러는 구조화+redact.
    자격은 env-only(config), 생성은 절대 raise 안 함(검증은 get_issue 시점)."""

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        token: str,
        http=None,
        acceptance_criteria_field: str = "",
    ):
        self._base_url = base_url
        self._email = email
        self._token = token
        self._acceptance_criteria_field = acceptance_criteria_field
        if http is None:
            import httpx  # lazy: httpx는 런타임 의존

            http = httpx
        self._http = http

    def _get(self, url: str, **kwargs):
        stream = getattr(self._http, "stream", None)
        if stream is None:
            response = self._http.get(url, **kwargs)
            content = getattr(response, "content", None)
            if content is not None and len(content) > _MAX_RESPONSE_BYTES:
                raise JiraError("jira response too large")
            return response

        with stream("GET", url, follow_redirects=False, **kwargs) as response:
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > _MAX_RESPONSE_BYTES:
                raise JiraError("jira response too large")
            if response.status_code >= 400:
                return _BufferedResponse(response.status_code, b"")
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise JiraError("jira response too large")
                content.extend(chunk)
            return _BufferedResponse(response.status_code, bytes(content))

    def _redact(self, text: str) -> str:
        out = text or ""
        for secret in (self._token, self._email):
            if secret:
                out = out.replace(secret, "[redacted]")
        return out

    def get_issue(self, key: str) -> dict:
        base = _validate_base_url(self._base_url)  # SSRF at fetch
        if not _KEY_RE.fullmatch(key):  # strict key
            raise JiraError(f"invalid jira key: {key!r}")
        ac_field = self._acceptance_criteria_field
        if ac_field and not _AC_FIELD_RE.fullmatch(ac_field):
            raise JiraError("invalid jira acceptance criteria field")
        url = f"{base}/rest/api/3/issue/{key}"
        requested_fields = ["summary", "description"]
        if ac_field:
            requested_fields.append(ac_field)
        try:
            resp = self._get(
                url,
                params={"fields": ",".join(requested_fields)},
                auth=(self._email, self._token),
                timeout=_HTTP_TIMEOUT_SEC,
            )
        except Exception as e:  # network/timeout → redacted
            raise JiraError(self._redact(f"{type(e).__name__}: {e}")) from None
        if resp.status_code >= 400:
            raise JiraError(
                self._redact(f"jira HTTP {resp.status_code}"),
                http_status=resp.status_code,
            )
        try:
            data = resp.json()
            fields = data.get("fields", {}) or {}
            summary = (fields.get("summary") or "")[
                : config.MAX_CONTEXT_CHARS_PER_SOURCE
            ]
            description = _adf_to_text(fields.get("description"))[
                : config.MAX_CONTEXT_CHARS_PER_SOURCE
            ]
            acceptance_criteria = (
                _adf_to_text(fields.get(ac_field))[
                    : config.MAX_CONTEXT_CHARS_PER_SOURCE
                ]
                if ac_field
                else ""
            )
        except Exception as e:  # non-JSON/non-dict body → redacted, structured
            raise JiraError(
                self._redact(f"malformed jira response: {type(e).__name__}: {e}")
            ) from None
        return {
            "key": key,
            "summary": summary,
            "description": description,
            "acceptance_criteria": acceptance_criteria,
        }


def _adf_to_text(node) -> str:
    """Jira Cloud description는 ADF(문서 JSON) 또는 문자열. best-effort 평문 추출."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        parts = []
        if isinstance(node.get("text"), str):
            parts.append(node["text"])
        for child in node.get("content", []) or []:
            parts.append(_adf_to_text(child))
        return " ".join(p for p in parts if p)
    if isinstance(node, list):
        return " ".join(_adf_to_text(x) for x in node if x)
    return ""
