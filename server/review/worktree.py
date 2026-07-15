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
                msg = (
                    "; ".join(e for e in errors if e) or "commit not found after fetch"
                )
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


@contextmanager
def checkout(worktree, clone, *, local_path, full_name, sha, pr_number=None):
    """리뷰 소스 체크아웃을 연다. local_path가 있으면 기존 로컬 clone에서 worktree를
    만들고(빠름), 없으면 gh로 임시 clone한 뒤 그 위에서 worktree를 만든다(로컬 clone
    미의존). 임시 clone은 종료 시 통째로 제거한다. worktree 인자는 테스트 주입 가능한
    prepared_worktree(또는 fake)이며, PR head ref fetch·체크아웃은 그쪽이 담당한다."""
    if local_path:
        with worktree(Path(local_path), sha, pr_number) as wt:
            yield wt
        return
    if clone is None:
        raise WorktreePrepareError(f"{full_name}: local_path 미설정 + clone 미배선")
    tmp = Path(tempfile.mkdtemp(prefix="almighty-clone-"))
    try:
        repo_dir = tmp / "repo"
        clone(full_name, str(repo_dir))
        with worktree(repo_dir, sha, pr_number) as wt:
            yield wt
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def prune_orphans(repo: Path) -> None:
    """실패로 남은 orphan worktree 정리(worker 기동 시 호출)."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True
    )
