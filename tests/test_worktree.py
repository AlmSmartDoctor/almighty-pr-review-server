import subprocess
import tempfile
from pathlib import Path

import pytest

from server.review.worktree import prepared_worktree


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
    with pytest.raises(subprocess.CalledProcessError):
        with prepared_worktree(src, bad_sha):
            pass  # should never reach here
    assert _tmp_wt_dirs() == before  # no almighty-wt-* leaked
