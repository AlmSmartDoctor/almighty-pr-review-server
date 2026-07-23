import asyncio
import runpy
import subprocess
from pathlib import Path

import pytest

from server.review.snapshot import (
    ReviewSnapshotCleanupError,
    ReviewSnapshotError,
    prepared_plain_snapshot,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    return repo


def _commit(repo: Path):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)


def test_plain_snapshot_contains_tracked_head_without_git_history(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n")
    _commit(repo)

    with prepared_plain_snapshot(repo) as snapshot:
        app = snapshot / "src" / "app.py"
        assert app.read_text() == "print('ok')\n"
        assert app.stat().st_mode & 0o222 == 0
        assert (snapshot / "src").stat().st_mode & 0o222 == 0
        assert snapshot.stat().st_mode & 0o222 == 0
        assert not (snapshot / ".git").exists()
        probe = subprocess.run(
            ["git", "-C", str(snapshot), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        assert probe.returncode != 0

    assert not snapshot.exists()


def test_plain_snapshot_rejects_symlink_escape(tmp_path):
    repo = _repo(tmp_path)
    (repo / "escape").symlink_to("../outside.txt")
    _commit(repo)

    with pytest.raises(ReviewSnapshotError, match="unsafe path or link"):
        with prepared_plain_snapshot(repo):
            pass


def test_plain_snapshot_enforces_content_cap_before_archive_write(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    (repo / "large.txt").write_text("x" * 100)
    _commit(repo)
    monkeypatch.setattr("server.config.REVIEW_SNAPSHOT_MAX_TOTAL_BYTES", 32)
    archive_calls = 0

    def must_not_archive(*args, **kwargs):
        nonlocal archive_calls
        archive_calls += 1
        raise AssertionError("archive must not run after preflight rejection")

    monkeypatch.setattr(
        "server.review.snapshot._create_archive_bounded", must_not_archive
    )
    with pytest.raises(ReviewSnapshotError, match="content exceeds"):
        with prepared_plain_snapshot(repo):
            pass
    assert archive_calls == 0


def test_plain_snapshot_archive_writer_stops_at_byte_cap(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "data.txt").write_text("x" * 10_000)
    _commit(repo)
    monkeypatch.setattr("server.config.REVIEW_SNAPSHOT_MAX_ARCHIVE_BYTES", 128)

    with pytest.raises(ReviewSnapshotError, match="archive exceeds byte cap"):
        with prepared_plain_snapshot(repo):
            pass


def test_plain_snapshot_cleanup_failure_is_not_silent(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("print('ok')\n")
    _commit(repo)
    real_rmtree = __import__("shutil").rmtree
    root = None

    def fail_rmtree(path):
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr("server.review.snapshot.shutil.rmtree", fail_rmtree)
    with pytest.raises(ReviewSnapshotCleanupError) as caught:
        with prepared_plain_snapshot(repo) as snapshot:
            root = snapshot.parent

    assert caught.value.safe_error_code == "snapshot_cleanup_failed"
    assert root is not None and root.exists()
    real_rmtree(root)


def test_snapshot_cleanup_failure_does_not_mask_cancellation(
    tmp_path, monkeypatch, capsys
):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("print('ok')\n")
    _commit(repo)
    real_rmtree = __import__("shutil").rmtree
    root = None

    monkeypatch.setattr(
        "server.review.snapshot.shutil.rmtree",
        lambda path: (_ for _ in ()).throw(OSError("simulated cleanup failure")),
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        with prepared_plain_snapshot(repo) as snapshot:
            root = snapshot.parent
            raise asyncio.CancelledError

    assert "snapshot_cleanup_failed" in getattr(caught.value, "__notes__", [])
    assert "snapshot_cleanup_failed" in capsys.readouterr().out
    assert root is not None and root.exists()
    real_rmtree(root)


def test_containment_preflight_unavailable_summary_is_failure():
    script = Path(__file__).parents[1] / "scripts" / "review-read-containment-preflight.py"
    namespace = runpy.run_path(str(script))
    exit_status = namespace["_summary_exit_code"]

    assert exit_status(0, "probe_passed") == 0
    assert exit_status(0, "unproven") == 0
    assert exit_status(0, "unavailable") == 1
    assert exit_status(None, "unavailable") == 1
