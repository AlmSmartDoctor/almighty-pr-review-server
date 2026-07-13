from server.context.base import ContextRequest, ContextResult


class GraphifyProvider:
    """프로젝트 컨텍스트 애그리게이터 — 리뷰 대상 레포 관련 정보(레포 개요 → DB 특징 →
    전체 프로젝트 진행상황)를 점진적으로 담는다. graph_source(req)->str 를 주입받아 렌더한다.
    소스 미주입=skipped, 실패=empty. NEVER raises (best-effort degrade)."""

    name = "graphify"

    def __init__(self, *, graph_source=None):
        self._source = graph_source

    def fetch(self, req: ContextRequest) -> ContextResult:
        if self._source is None:
            return ContextResult(provider=self.name, status="skipped", text="")
        try:
            text = self._source(req) or ""
        except Exception:  # 소스 미도달/오류 → best-effort degrade
            return ContextResult(provider=self.name, status="empty", text="")
        status = "ok" if text.strip() else "empty"
        return ContextResult(provider=self.name, status=status, text=text)
