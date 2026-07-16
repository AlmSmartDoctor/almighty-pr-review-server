_SLACK_API = "https://slack.com/api"
_HTTP_TIMEOUT_SEC = 5


class SlackError(Exception):
    """Slack API 실패. 메시지는 항상 봇 토큰이 제거된 상태(_redact)."""

    def __init__(self, message: str, *, http_status: "int | None" = None):
        super().__init__(message)
        self.http_status = http_status


class SlackClient:
    """chat.postMessage 아웃바운드 클라이언트. JiraClient의 injected-http seam을 그대로
    따르되 인증만 Bearer 헤더 + JSON POST로 바꾼다(Slack 규약). 모든 실패 경로는 토큰을
    제거한 SlackError로 감싼다. Slack은 논리 실패도 HTTP 200 + {"ok": false}로 주므로
    status_code만이 아니라 ok 필드도 검사한다."""

    def __init__(self, *, token: str, http=None):
        self._token = token
        if http is None:
            import httpx  # lazy: httpx는 런타임 의존(pyproject에 이미 선언)

            http = httpx
        self._http = http

    def _redact(self, text: str) -> str:
        out = text or ""
        if self._token:
            out = out.replace(self._token, "[redacted]")
        return out

    def post_message(self, *, channel: str, text: str) -> dict:
        """채널에 메시지 게시. 성공 시 {"channel", "ts"} 반환(반응 매핑 키)."""
        try:
            resp = self._http.post(
                f"{_SLACK_API}/chat.postMessage",
                json={"channel": channel, "text": text},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=_HTTP_TIMEOUT_SEC,
            )
        except Exception as e:  # 네트워크/타임아웃 — 토큰 제거 후 래핑
            raise SlackError(self._redact(f"slack request failed: {e}")) from None
        status = getattr(resp, "status_code", None)
        if status is not None and status >= 400:
            raise SlackError(self._redact(f"slack http {status}"), http_status=status)
        try:
            data = resp.json()
        except Exception as e:
            raise SlackError(self._redact(f"slack malformed response: {e}")) from None
        if not isinstance(data, dict) or not data.get("ok"):
            err = data.get("error") if isinstance(data, dict) else "unknown"
            raise SlackError(self._redact(f"slack error: {err}"))
        return {"channel": data.get("channel"), "ts": data.get("ts")}
