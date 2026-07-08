import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


@contextmanager
def prepared_worktree(repo: Path, sha: str):
    """repo의 특정 sha를 detached worktree로 체크아웃. 종료 시 제거(★개정: shutil)."""
    repo = Path(repo)
    tmp = Path(tempfile.mkdtemp(prefix="almighty-wt-"))
    wt = tmp / "wt"
    try:
        _git(repo, "worktree", "add", "--detach", str(wt), sha)
        yield wt
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
            capture_output=True,
            text=True,
        )
        shutil.rmtree(tmp, ignore_errors=True)  # ★개정: rm -rf 서브프로세스 대신


def prune_orphans(repo: Path) -> None:
    """실패로 남은 orphan worktree 정리(worker 기동 시 호출)."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True
    )
