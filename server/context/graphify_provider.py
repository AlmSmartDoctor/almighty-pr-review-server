from server.context.base import ContextRequest, ContextResult


class GraphifyProvider:
    """코드 그래프 컨텍스트 provider — 통합 대상 아티팩트/엔드포인트가 현재 전무하여
    항상 skipped 를 반환하는 스텁. 인터페이스만 확보(향후 B-next에서 구현)."""

    name = "graphify"

    def fetch(self, req: ContextRequest) -> ContextResult:
        return ContextResult(provider=self.name, status="skipped", text="")
