import subprocess
import tempfile
from pathlib import Path

import pytest

from server.review.worktree import WorktreePrepareError, prepared_worktree


def _init_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "f.txt").write_text("hello")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def test_worktree_created_and_cleaned(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    sha = _init_repo(src)
    with prepared_worktree(src, sha) as wt:
        assert (wt / "f.txt").read_text() == "hello"
        wt_path = wt
    # 컨텍스트 종료 후 worktree 제거됨
    assert not wt_path.exists()


def _tmp_wt_dirs():
    return set(Path(tempfile.gettempdir()).glob("almighty-wt-*"))


def test_setup_failure_cleans_up_tmpdir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _init_repo(src)
    before = _tmp_wt_dirs()
    bad_sha = "0" * 40  # valid-looking but nonexistent object
    with pytest.raises(WorktreePrepareError):
        with prepared_worktree(src, bad_sha):
            pass  # should never reach here
    assert _tmp_wt_dirs() == before  # no almighty-wt-* leaked


def test_worktree_fetches_missing_commit(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    src = tmp_path / "src"
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    (seed / "f.txt").write_text("hello")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "HEAD:main"], check=True)
    subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(src)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)

    (other / "g.txt").write_text("fetched")
    subprocess.run(["git", "-C", str(other), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(other),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "second",
        ],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(other), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "HEAD:main"], check=True)
    assert subprocess.run(
        ["git", "-C", str(src), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        text=True,
    ).returncode != 0

    with prepared_worktree(src, sha) as wt:
        assert (wt / "g.txt").read_text() == "fetched"


def test_worktree_fetches_missing_commit_from_pr_ref(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    src = tmp_path / "src"
    pr_src = tmp_path / "pr-src"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    (seed / "f.txt").write_text("hello")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "HEAD:main"], check=True)
    subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(src)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(pr_src)], check=True)

    (pr_src / "pr.txt").write_text("from pr")
    subprocess.run(["git", "-C", str(pr_src), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(pr_src),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "pr",
        ],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(pr_src), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(pr_src), "push", "-q", "origin", "HEAD:refs/pull/123/head"],
        check=True,
    )
    assert subprocess.run(
        ["git", "-C", str(src), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        text=True,
    ).returncode != 0

    with prepared_worktree(src, sha, pr_number=123) as wt:
        assert (wt / "pr.txt").read_text() == "from pr"
