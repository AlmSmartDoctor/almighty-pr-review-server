import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from server import config


class WorktreePrepareError(RuntimeError):
    pass


def _clone_dir(full_name: str) -> Path:
    """레포별 서비스 전용 clone 경로. CLONE_DIR/<owner>__<repo>."""
    return config.CLONE_DIR / full_name.replace("/", "__")


def persistent_clone(clone, full_name: str) -> Path:
    """서비스 전용 영구 clone을 확보해 경로를 돌려준다. 없으면 새로 clone하고,
    있으면 재사용한다(재리뷰마다 재-clone하지 않아 빠름). PR head sha fetch는
    prepared_worktree가 담당하므로 여기선 clone 존재만 보장한다. worker는 job을
    순차 처리하므로 같은 clone에 동시 git 작업은 없다(락 불필요)."""
    dest = _clone_dir(full_name)
    if (dest / ".git").exists():
        prune_orphans(dest)  # 크래시로 남은 orphan worktree 자가 정리
        return dest
    config.CLONE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(dest, ignore_errors=True)  # .git 없는 반쪽 clone 잔재 제거
    clone(full_name, str(dest))
    return dest


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


def _fetch_base_revision(repo: Path, base_ref: str, base_sha: str = "") -> None:
    """참조문서용 base branch와 poll/webhook 시점의 정확한 commit을 확보한다."""
    if (
        not base_ref
        or base_ref.startswith("-")
        or ".." in base_ref
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", base_ref)
    ):
        return
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "fetch",
                "origin",
                f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}",
                "--depth=1",
            ],
            capture_output=True,
            text=True,
            timeout=config.GH_TIMEOUT_SEC,
        )
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", base_sha) and not _has_commit(
            repo, base_sha
        ):
            subprocess.run(
                ["git", "-C", str(repo), "fetch", "origin", base_sha, "--depth=1"],
                capture_output=True,
                text=True,
                timeout=config.GH_TIMEOUT_SEC,
            )
    except (OSError, subprocess.TimeoutExpired):
        pass  # provider가 정확한 base revision 미도달을 error로 self-degrade한다.


@contextmanager
def checkout(
    worktree,
    clone,
    *,
    local_path,
    full_name,
    sha,
    pr_number=None,
    base_ref: str | None = None,
    base_sha: str | None = None,
):
    """리뷰 소스 체크아웃을 연다. 기본은 서비스 전용 영구 clone(CLONE_DIR)에서 worktree를
    떠 사용자의 라이브 체크아웃에 의존하지 않는다(작업 경로가 실시간으로 바뀌어도 안전).
    local_path가 지정되면(고급 옵션) 그 기존 clone을 소스로 쓴다. worktree 인자는 테스트
    주입 가능한 prepared_worktree(또는 fake)이며 PR head ref fetch·체크아웃은 그쪽이 담당."""
    if local_path:
        repo_dir = Path(local_path)
    else:
        if clone is None:
            raise WorktreePrepareError(f"{full_name}: local_path 미설정 + clone 미배선")
        repo_dir = persistent_clone(clone, full_name)
    if base_ref:
        _fetch_base_revision(repo_dir, base_ref, base_sha or "")
    with worktree(repo_dir, sha, pr_number) as wt:
        yield wt


def prune_orphans(repo: Path) -> None:
    """실패/크래시로 남은 orphan worktree 정리(영구 clone 재사용 직전 호출)."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True
    )
