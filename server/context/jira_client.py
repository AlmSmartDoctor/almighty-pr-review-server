import ipaddress
import re
from urllib.parse import urlparse

from server import config

_HTTP_TIMEOUT_SEC = 10
_KEY_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")


class JiraError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


def _validate_base_url(base_url: str) -> str:
    """B-INV-7: fetch-time SSRF guard. base_url은 operator가 설정하는 env(공격자 입력 아님) —
    유일한 공격자-유래 입력은 issue key(아래에서 별도 엄격 검증)이므로, DNS 조회 없는
    denylist로 충분하며 테스트를 오프라인/결정적으로 유지할 수 있다.
    https 스킴을 강제하고, host가 IP 리터럴이면 private/loopback/link-local/reserved/
    multicast/unspecified를 차단(127.0.0.1, 10/8, 192.168/16, 169.254.169.254 메타데이터 등),
    host가 이름이면 localhost/*.local/*.internal을 차단한다. DNS는 resolve하지 않는다."""
    p = urlparse(base_url)
    if p.scheme != "https" or not p.hostname:
        raise JiraError("jira base_url must be https with a host")
    host = p.hostname
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise JiraError("jira base_url host not allowed")
    elif host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise JiraError("jira base_url host not allowed")
    return base_url.rstrip("/")


class JiraClient:
    """gh.py 규율을 미러: 주입 http로 테스트 가능, 에러는 구조화+redact.
    자격은 env-only(config), 생성은 절대 raise 안 함(검증은 get_issue 시점)."""

    def __init__(self, *, base_url: str, email: str, token: str, http=None):
        self._base_url = base_url
        self._email = email
        self._token = token
        if http is None:
            import httpx  # lazy: httpx는 런타임 의존

            http = httpx
        self._http = http

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
        url = f"{base}/rest/api/3/issue/{key}"
        try:
            resp = self._http.get(
                url,
                params={"fields": "summary,description"},
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
        data = resp.json()
        fields = data.get("fields", {}) or {}
        summary = (fields.get("summary") or "")[: config.MAX_CONTEXT_CHARS_PER_SOURCE]
        description = _adf_to_text(fields.get("description"))[
            : config.MAX_CONTEXT_CHARS_PER_SOURCE
        ]
        return {"key": key, "summary": summary, "description": description}


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
