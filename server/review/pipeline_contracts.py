from dataclasses import dataclass, field
from typing import Callable

from server.models import Finding
from server.review.runner import RunnerPool
from server.seams import NoOpContextProvider


REVIEW_CHUNKER_VERSION = "char-v1"


def row_value(row, key, default):
    """Return a configured row value, falling back for NULL and empty strings."""
    value = row[key] if key in row.keys() else None
    return value if value not in (None, "") else default


@dataclass
class PipelineDeps:
    gh_diff: Callable[[str, int], str]
    worktree: Callable  # contextmanager(repo, sha, pr_number) -> path
    adapters: list  # vendor adapters (.vendor, async .review())
    prescreen: Callable[
        [str, str], tuple
    ]  # (diff, model) -> (complexity, score, reason)
    repo_local_path: str
    clone: Callable = (
        None  # (full_name, dest)->None; local_path 없을 때 서비스 전용 clone
    )
    context: object = field(default_factory=NoOpContextProvider)
    pool: RunnerPool = None  # ★개정: 벤더 병렬 실행 세마포어(없으면 생성)
    gh_compare_diff: Callable = None  # (repo, base, head)->diff; None=증분 미지원
    verify: object = (
        None  # async (targets, VerifyContext) -> list[Verdict]; None=미배선
    )
    snapshot: Callable | None = None  # contextmanager(worktree)->plain tracked cwd


@dataclass(frozen=True)
class PromptChunk:
    index: int
    prompt: str
    diff_text: str
    diff_hash: str
    context_hash: str
    prompt_nonce: str = "00000000"
    owned_changed_lines: dict[str, frozenset[int]] = field(default_factory=dict)
    chunker_version: str = REVIEW_CHUNKER_VERSION


@dataclass
class VendorRunResult:
    vendor: str
    status: str  # done|partial|failed|timeout
    findings: list[Finding]
    duration_ms: int
    chunks: list[dict]
    verify_chunks: list[dict] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status in {"done", "partial"}
