"""Plain tracked-file snapshots for review model working directories.

This removes Git metadata from the model's normal cwd to reduce accidental history/ref
exploration. It is defense-in-depth, not an OS-level read-containment boundary.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from server import config
from server.review.vendors import run_bounded_process_sync

_LS_TREE_MAX_BYTES = 64 * 1024 * 1024


class ReviewSnapshotError(RuntimeError):
    safe_error_code = "snapshot_failed"


class ReviewSnapshotCleanupError(ReviewSnapshotError):
    safe_error_code = "snapshot_cleanup_failed"


def _safe_member(member: tarfile.TarInfo) -> bool:
    path = PurePosixPath(member.name)
    if not member.name or path.is_absolute() or ".." in path.parts:
        return False
    if member.isdev() or member.isfifo():
        return False
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname)
        if target.is_absolute():
            return False
        combined = posixpath.normpath(
            posixpath.join(posixpath.dirname(member.name), member.linkname)
        )
        if combined == ".." or combined.startswith("../"):
            return False
    return True


def _validate_tree_budget(worktree: Path) -> None:
    try:
        result = run_bounded_process_sync(
            ["git", "-C", str(worktree), "ls-tree", "-r", "-l", "-z", "HEAD"],
            timeout=config.REVIEW_SNAPSHOT_TIMEOUT_SEC,
            stream_limit=_LS_TREE_MAX_BYTES,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewSnapshotError("snapshot tree preflight failed") from exc
    if (
        result.exit_code != 0
        or result.stdout_truncated
        or result.stderr_truncated
    ):
        raise ReviewSnapshotError("snapshot tree preflight failed")
    files = 0
    total = 0
    for record in result.stdout.split("\0"):
        if not record:
            continue
        try:
            header, _path = record.split("\t", 1)
            fields = header.split()
            size_field = fields[3]
        except (ValueError, IndexError) as exc:
            raise ReviewSnapshotError("snapshot tree preflight invalid") from exc
        files += 1
        if files > config.REVIEW_SNAPSHOT_MAX_FILES:
            raise ReviewSnapshotError("snapshot file count exceeds cap")
        if size_field == "-":
            continue
        try:
            size = int(size_field)
        except ValueError as exc:
            raise ReviewSnapshotError("snapshot tree preflight invalid") from exc
        if size > config.REVIEW_SNAPSHOT_MAX_FILE_BYTES:
            raise ReviewSnapshotError("snapshot file exceeds cap")
        total += size
        if total > config.REVIEW_SNAPSHOT_MAX_TOTAL_BYTES:
            raise ReviewSnapshotError("snapshot content exceeds cap")


def _kill_archive_process(proc) -> None:
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _create_archive_bounded(worktree: Path, archive_path: Path) -> None:
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(worktree), "archive", "--format=tar", "HEAD"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        raise ReviewSnapshotError("snapshot archive command failed") from exc
    state = {"overflow": False, "write_failed": False}

    def write_archive():
        written = 0
        try:
            with archive_path.open("wb") as output:
                while True:
                    chunk = proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    if written + len(chunk) > config.REVIEW_SNAPSHOT_MAX_ARCHIVE_BYTES:
                        state["overflow"] = True
                        _kill_archive_process(proc)
                        break
                    output.write(chunk)
                    written += len(chunk)
        except (OSError, ValueError):
            state["write_failed"] = True
            _kill_archive_process(proc)

    writer = threading.Thread(target=write_archive, daemon=True)
    writer.start()
    try:
        proc.wait(timeout=config.REVIEW_SNAPSHOT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        _kill_archive_process(proc)
        proc.wait()
        raise ReviewSnapshotError("snapshot archive command failed") from exc
    finally:
        writer.join(timeout=1)
        if writer.is_alive():
            _kill_archive_process(proc)
            proc.stdout.close()
            writer.join(timeout=1)
    if state["overflow"]:
        raise ReviewSnapshotError("snapshot archive exceeds byte cap")
    if state["write_failed"] or writer.is_alive() or proc.returncode != 0:
        raise ReviewSnapshotError("snapshot archive command failed")


def _validate_archive(path: Path) -> None:
    try:
        archive_bytes = path.stat().st_size
    except OSError as exc:
        raise ReviewSnapshotError("snapshot archive unavailable") from exc
    if archive_bytes > config.REVIEW_SNAPSHOT_MAX_ARCHIVE_BYTES:
        raise ReviewSnapshotError("snapshot archive exceeds byte cap")
    files = 0
    total = 0
    try:
        with tarfile.open(path, mode="r:") as archive:
            for member in archive.getmembers():
                files += 1
                if files > config.REVIEW_SNAPSHOT_MAX_FILES:
                    raise ReviewSnapshotError("snapshot file count exceeds cap")
                if not _safe_member(member):
                    raise ReviewSnapshotError("snapshot contains unsafe path or link")
                if member.isfile():
                    if member.size > config.REVIEW_SNAPSHOT_MAX_FILE_BYTES:
                        raise ReviewSnapshotError("snapshot file exceeds cap")
                    total += member.size
                    if total > config.REVIEW_SNAPSHOT_MAX_TOTAL_BYTES:
                        raise ReviewSnapshotError("snapshot content exceeds cap")
    except (OSError, tarfile.TarError) as exc:
        raise ReviewSnapshotError("snapshot archive is invalid") from exc


def _extract_archive(archive_path: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            # Python's data filter additionally rejects device files and path escapes.
            archive.extractall(destination, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise ReviewSnapshotError("snapshot extraction failed") from exc


def _set_tree_read_only(destination: Path) -> None:
    for path in destination.rglob("*"):
        if path.is_symlink():
            continue
        try:
            mode = path.stat().st_mode & 0o777
            path.chmod(mode & ~0o222)
        except OSError as exc:
            raise ReviewSnapshotError("snapshot read-only lock failed") from exc
    destination.chmod(destination.stat().st_mode & 0o555)


def _restore_tree_writable(destination: Path) -> None:
    if not destination.exists():
        return
    failed = False
    for path in [destination, *destination.rglob("*")]:
        if path.is_dir() and not path.is_symlink():
            try:
                path.chmod(0o700)
            except OSError:
                failed = True
    if failed:
        raise ReviewSnapshotCleanupError("snapshot cleanup failed")


@contextmanager
def prepared_plain_snapshot(worktree: Path):
    """Yield a temporary tracked-file tree without `.git`, refs, or object history."""
    worktree = Path(worktree)
    root = Path(tempfile.mkdtemp(prefix="almighty-snapshot-"))
    archive_path = root / "snapshot.tar"
    destination = root / "files"
    destination.mkdir()
    active_error = None
    try:
        _validate_tree_budget(worktree)
        _create_archive_bounded(worktree, archive_path)
        _validate_archive(archive_path)
        _extract_archive(archive_path, destination)
        try:
            archive_path.unlink(missing_ok=True)
        except OSError as exc:
            raise ReviewSnapshotError("snapshot archive cleanup failed") from exc
        if (destination / ".git").exists():
            raise ReviewSnapshotError("snapshot unexpectedly contains git metadata")
        _set_tree_read_only(destination)
        yield destination
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        cleanup_error = None
        try:
            _restore_tree_writable(destination)
        except ReviewSnapshotCleanupError as exc:
            cleanup_error = exc
        try:
            shutil.rmtree(root)
        except OSError as exc:
            cleanup_error = ReviewSnapshotCleanupError(
                "snapshot cleanup failed"
            )
            cleanup_error.__cause__ = exc
        if root.exists():
            cleanup_error = ReviewSnapshotCleanupError(
                "snapshot cleanup left residual files"
            )
        if cleanup_error is not None:
            if active_error is None:
                raise cleanup_error
            active_error.add_note(cleanup_error.safe_error_code)
            print(f"[snapshot] {cleanup_error.safe_error_code}")
