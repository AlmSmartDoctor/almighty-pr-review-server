from server.context.base import ContextRequest, ContextResult


class SourceBackedProvider:
    """source(req)->str 를 주입받아 렌더하는 공통 provider(db_schema/graphify/
    team_feedback이 공유). 소스 미주입=skipped, 실패=empty. NEVER raises
    (best-effort degrade)."""

    def __init__(self, name: str, *, source=None):
        self.name = name
        self._source = source

    def fetch(self, req: ContextRequest) -> ContextResult:
        if self._source is None:
            return ContextResult(provider=self.name, status="skipped", text="")
        try:
            text = self._source(req) or ""
        except Exception:  # 소스 미도달/오류 → best-effort degrade
            return ContextResult(provider=self.name, status="empty", text="")
        status = "ok" if text.strip() else "empty"
        return ContextResult(provider=self.name, status=status, text=text)
