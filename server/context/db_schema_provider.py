from server.context.base import ContextRequest, ContextResult


class DBSchemaProvider:
    """변경 파일 관련 테이블 스키마(DDL)를 주입하는 provider. 실 DB 접근은 유예 —
    schema_source(req)->str 를 주입받아 렌더한다. 소스 미주입=skipped, 실패=empty.
    NEVER raises (best-effort degrade)."""

    name = "db_schema"

    def __init__(self, *, schema_source=None):
        self._source = schema_source

    def fetch(self, req: ContextRequest) -> ContextResult:
        if self._source is None:
            return ContextResult(provider=self.name, status="skipped", text="")
        try:
            text = self._source(req) or ""
        except Exception:  # 소스 미도달/오류 → best-effort degrade
            return ContextResult(provider=self.name, status="empty", text="")
        status = "ok" if text.strip() else "empty"
        return ContextResult(provider=self.name, status=status, text=text)
