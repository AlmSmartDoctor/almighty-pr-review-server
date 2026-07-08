from dataclasses import dataclass


class NoOpContextProvider:
    """v1 no-op. B에서 Jira/DB/Graphify 주입 지점."""

    def gather(self, *, repo: str, pr_number: int) -> str:
        return ""


@dataclass
class LocalIdentity:
    """v1 = 나 = 내 gh 시트. team-mode에서 per-user로 교체."""

    actor: str = "me"


class NullMemoryStore:
    """v1 = 저장만(no-op). C에서 학습 신호 소비."""

    def record(self, *, event: str, payload: dict) -> None:
        return None
