from server.context.base import ContextRequest


class NoOpContextProvider:
    """v1 no-op. B에서 CompositeContextProvider(레지스트리)로 대체."""

    def gather(self, *, req: ContextRequest) -> str:
        return ""
