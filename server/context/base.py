from dataclasses import dataclass
from typing import Protocol

from server import config


@dataclass(frozen=True)
class ContextRequest:
    repo: str
    pr_number: int
    title: str = ""
    author: str = ""
    head_ref: str = ""
    base_ref: str = ""
    body: str = ""
    changed_files: tuple = ()


@dataclass
class ContextResult:
    provider: str
    status: str  # "ok" | "empty" | "error" | "skipped"
    text: str = ""
    meta: dict | None = None
    error: str | None = None


class ContextProvider(Protocol):
    """개별 소스 계약: concrete provider(Static/Jira/...)가 구현하는 fetch 인터페이스.
    이것을 CompositeContextProvider(gather 집계 seam)와 혼동하지 말 것."""

    name: str

    def fetch(self, req: ContextRequest) -> ContextResult: ...


def redact_secrets(text: str) -> str:
    """config의 비밀 값(있을 때만)을 [redacted]로 치환. 빈 값은 무시(전체 치환 방지)."""
    for secret in (config.JIRA_API_TOKEN, config.JIRA_EMAIL):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text
