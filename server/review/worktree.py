import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


class WorktreePrepareError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _try_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def _has_commit(repo: Path, sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


@contextmanager
def prepared_worktree(repo: Path, sha: str, pr_number: int | None = None):
    """repo의 특정 sha를 detached worktree로 체크아웃. 종료 시 제거(★개정: shutil)."""
    repo = Path(repo)
    tmp = Path(tempfile.mkdtemp(prefix="almighty-wt-"))
    wt = tmp / "wt"
    try:
        if not _has_commit(repo, sha):
            errors = []
            if pr_number is not None:
                ref = f"+pull/{pr_number}/head:refs/almighty/pr/{pr_number}"
                res = _try_git(repo, "fetch", "origin", ref, "--depth=1")
                if res.returncode != 0:
                    errors.append(f"PR head fetch failed: {res.stderr.strip()}")
            if not _has_commit(repo, sha):
                res = _try_git(repo, "fetch", "origin", sha, "--depth=1")
                if res.returncode != 0:
                    errors.append(f"sha fetch failed: {res.stderr.strip()}")
            if not _has_commit(repo, sha):
                msg = "; ".join(e for e in errors if e) or "commit not found after fetch"
                raise WorktreePrepareError(f"PR head fetch failed for {sha}: {msg}")
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
