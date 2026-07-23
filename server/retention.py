import asyncio
import time
from pathlib import Path


def _confined(path: str, root: Path) -> Path | None:
    try:
        candidate = Path(path).resolve()
        base = root.resolve()
        candidate.relative_to(base)
        return candidate
    except (OSError, ValueError):
        return None


def cleanup(conn, *, retention_days: int, raw_dir: Path, batch_size: int = 100) -> dict:
    """오래 닫힌 PR의 실행 이력과 confined raw 파일을 opt-in으로 bounded 정리한다."""
    if retention_days <= 0:
        return {"prs": 0, "raw_files": 0}
    cutoff = f"-{retention_days} days"
    rows = conn.execute(
        """SELECT p.id FROM pull_request p
           WHERE p.state<>'open' AND p.updated_at IS NOT NULL
             AND p.updated_at < datetime('now', ?)
             AND NOT EXISTS (SELECT 1 FROM review_job j WHERE j.pr_id=p.id
                             AND j.status IN ('queued','running'))
             AND NOT EXISTS (SELECT 1 FROM review_run rr WHERE rr.pr_id=p.id
                             AND rr.status='running')
           ORDER BY p.id LIMIT ?""",
        (cutoff, batch_size),
    ).fetchall()
    raw_paths: list[str] = []
    deleted_prs = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            pid = row["id"]
            # 후보 조회 뒤 PR reopen/job enqueue가 일어났을 수 있으므로 write lock 아래서
            # 적격성을 다시 확인한다.
            eligible = conn.execute(
                """SELECT 1 FROM pull_request p
                   WHERE p.id=? AND p.state<>'open' AND p.updated_at IS NOT NULL
                     AND p.updated_at < datetime('now', ?)
                     AND NOT EXISTS (SELECT 1 FROM review_job j WHERE j.pr_id=p.id
                                     AND j.status IN ('queued','running'))
                     AND NOT EXISTS (SELECT 1 FROM review_run rr WHERE rr.pr_id=p.id
                                     AND rr.status='running')""",
                (pid, cutoff),
            ).fetchone()
            if eligible is None:
                continue
            run_ids = [
                r[0] for r in conn.execute(
                    "SELECT id FROM review_run WHERE pr_id=?", (pid,)
                ).fetchall()
            ]
            if run_ids:
                marks = ",".join("?" for _ in run_ids)
                raw_paths.extend(
                    r[0] for r in conn.execute(
                        f"SELECT raw_path FROM vendor_result WHERE run_id IN ({marks}) AND raw_path IS NOT NULL",
                        run_ids,
                    ).fetchall()
                )
                conn.execute(
                    f"DELETE FROM finding_decision WHERE finding_id IN "
                    f"(SELECT id FROM finding WHERE run_id IN ({marks}))", run_ids
                )
                for table in (
                    "feedback_signal", "slack_post", "posted_comment",
                    "github_post_operation", "finding", "vendor_result",
                ):
                    conn.execute(
                        f"DELETE FROM {table} WHERE run_id IN ({marks})", run_ids
                    )
            conn.execute("DELETE FROM review_job WHERE pr_id=?", (pid,))
            conn.execute("DELETE FROM review_run WHERE pr_id=?", (pid,))
            conn.execute("DELETE FROM pre_screen WHERE pr_id=?", (pid,))
            conn.execute("DELETE FROM pull_request WHERE id=?", (pid,))
            deleted_prs += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    deleted_files = 0
    for value in raw_paths:
        path = _confined(value, raw_dir)
        if path is not None:
            try:
                existed = path.exists()
                path.unlink(missing_ok=True)
                deleted_files += int(existed)
            except OSError:
                pass

    # DB commit 직후 crash/unlink 실패로 남은 orphan도 다음 주기에 다시 발견한다.
    referenced = {
        str(Path(row[0]).resolve())
        for row in conn.execute(
            "SELECT raw_path FROM vendor_result WHERE raw_path IS NOT NULL"
        ).fetchall()
    }
    cutoff_epoch = time.time() - retention_days * 86_400
    if raw_dir.exists():
        for path in raw_dir.rglob("*"):
            try:
                if (
                    path.is_file()
                    and str(path.resolve()) not in referenced
                    and path.stat().st_mtime < cutoff_epoch
                ):
                    path.unlink()
                    deleted_files += 1
            except OSError:
                pass
    return {"prs": deleted_prs, "raw_files": deleted_files}


def cleanup_raw_diagnostics(
    conn, *, retention_days: int, raw_dir: Path
) -> dict:
    """Delete legacy raw transcripts on an independent positive TTL.

    New reviews no longer create raw files. This bounded sweep removes referenced legacy
    files and clears their DB paths without deleting review history.
    """
    cutoff_epoch = time.time() - retention_days * 86_400
    cleared_ids = []
    deleted_files = 0
    rows = conn.execute(
        "SELECT id, raw_path FROM vendor_result WHERE raw_path IS NOT NULL"
    ).fetchall()
    for row in rows:
        path = _confined(row["raw_path"], raw_dir)
        if path is None:
            continue
        try:
            old_or_missing = not path.exists() or path.stat().st_mtime < cutoff_epoch
            if not old_or_missing:
                continue
            existed = path.exists()
            path.unlink(missing_ok=True)
            deleted_files += int(existed)
            cleared_ids.append(row["id"])
        except OSError:
            continue
    if cleared_ids:
        marks = ",".join("?" for _ in cleared_ids)
        conn.execute(
            f"UPDATE vendor_result SET raw_path=NULL WHERE id IN ({marks})",
            cleared_ids,
        )
        conn.commit()

    referenced = {
        str(Path(row[0]).resolve())
        for row in conn.execute(
            "SELECT raw_path FROM vendor_result WHERE raw_path IS NOT NULL"
        ).fetchall()
    }
    if raw_dir.exists():
        for path in raw_dir.rglob("*"):
            try:
                resolved = str(path.resolve())
                if (
                    path.is_file()
                    and resolved not in referenced
                    and path.stat().st_mtime < cutoff_epoch
                ):
                    path.unlink()
                    deleted_files += 1
            except OSError:
                pass
    return {"raw_files": deleted_files, "paths_cleared": len(cleared_ids)}


def cleanup_context_payloads(conn, *, retention_days: int) -> dict:
    """Expire context text while retaining content-free manifests and metrics."""
    cursor = conn.execute(
        """UPDATE review_run
           SET context_text=NULL, context_chunks=NULL
           WHERE status<>'running' AND finished_at IS NOT NULL
             AND finished_at < datetime('now', ?)
             AND (context_text IS NOT NULL OR context_chunks IS NOT NULL)""",
        (f"-{retention_days} days",),
    )
    conn.commit()
    return {"runs_cleared": cursor.rowcount}


async def diagnostic_cleanup_loop(
    db_path,
    *,
    retention_days: int,
    raw_dir: Path,
    stop_event,
    context_retention_days: int | None = None,
):
    from server.db import connect

    while not stop_event.is_set():
        conn = connect(db_path)
        try:
            result = await asyncio.to_thread(
                cleanup_raw_diagnostics,
                conn,
                retention_days=retention_days,
                raw_dir=raw_dir,
            )
            if context_retention_days is not None:
                context_result = await asyncio.to_thread(
                    cleanup_context_payloads,
                    conn,
                    retention_days=context_retention_days,
                )
                if context_result["runs_cleared"]:
                    print(f"[retention] cleaned context payloads {context_result}")
            if result["raw_files"]:
                print(f"[retention] cleaned raw diagnostics {result}")
        except Exception as exc:
            print(f"[retention] raw cleanup failed: {type(exc).__name__}")
        finally:
            conn.close()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=86_400)
        except asyncio.TimeoutError:
            pass


async def cleanup_loop(db_path, *, retention_days: int, raw_dir: Path, stop_event):
    from server.db import connect

    while not stop_event.is_set():
        conn = connect(db_path)
        try:
            result = await asyncio.to_thread(
                cleanup, conn, retention_days=retention_days, raw_dir=raw_dir
            )
            if result["prs"]:
                print(f"[retention] cleaned {result}")
        except Exception as exc:
            print(f"[retention] cleanup failed: {exc!r}")
        finally:
            conn.close()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=86_400)
        except asyncio.TimeoutError:
            pass
