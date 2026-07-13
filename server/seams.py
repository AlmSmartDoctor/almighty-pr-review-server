from dataclasses import dataclass

from server.context.base import ContextRequest


class NoOpContextProvider:
    """v1 no-op. B에서 CompositeContextProvider(레지스트리)로 대체."""

    def gather(self, *, req: ContextRequest) -> str:
        return ""


@dataclass
class LocalIdentity:
    """v1 = 나 = 내 gh 시트. team-mode에서 per-user로 교체."""

    actor: str = "me"


class NullMemoryStore:
    """v1 = 저장만(no-op). C에서 학습 신호 소비."""

    def record(self, *, event: str, payload: dict) -> None:
        return None
