import sqlite3


DEFAULT_TTL_SECONDS = 60
LEGACY_STALE_MINUTES = 30


def register(conn, process_id: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
    conn.execute(
        """INSERT INTO process_lease
           (process_id, started_at, heartbeat_at, expires_at)
           VALUES (?, datetime('now'), datetime('now'),
                   datetime('now', ?))""",
        (process_id, f"+{ttl_seconds} seconds"),
    )
    conn.commit()


def heartbeat(conn, process_id: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    cur = conn.execute(
        """UPDATE process_lease
           SET heartbeat_at=datetime('now'), expires_at=datetime('now', ?)
           WHERE process_id=? AND expires_at > datetime('now')""",
        (f"+{ttl_seconds} seconds", process_id),
    )
    conn.commit()
    return cur.rowcount == 1


def release(conn, process_id: str) -> None:
    conn.execute("DELETE FROM process_lease WHERE process_id=?", (process_id,))
    conn.commit()


def recover_expired(conn, *, legacy_stale_minutes: int = LEGACY_STALE_MINUTES) -> dict:
    """만료된 process owner와 오래된 legacy(NULL owner) 작업만 회수한다."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        dead = """owner_process_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM process_lease pl
                    WHERE pl.process_id=owner_process_id
                      AND pl.expires_at > datetime('now'))"""
        legacy_job = "owner_process_id IS NULL AND locked_at <= datetime('now', ?)"
        stale_arg = f"-{legacy_stale_minutes} minutes"
        jobs = conn.execute(
            f"""UPDATE review_job SET status='queued', locked_by=NULL, locked_at=NULL,
                       owner_process_id=NULL, error='recovered from expired process lease'
                   WHERE status='running' AND (({dead}) OR ({legacy_job}))""",
            (stale_arg,),
        ).rowcount
        run_ids = [
            row[0]
            for row in conn.execute(
                f"""SELECT id FROM review_run WHERE status='running' AND (
                       ({dead}) OR
                       (owner_process_id IS NULL AND started_at <= datetime('now', ?)))""",
                (stale_arg,),
            ).fetchall()
        ]
        runs = 0
        if run_ids:
            marks = ",".join("?" for _ in run_ids)
            error = "worker process lease expired"
            conn.execute(
                f"UPDATE vendor_result SET status='failed', error=? "
                f"WHERE status='running' AND run_id IN ({marks})",
                (error, *run_ids),
            )
            runs = conn.execute(
                f"""UPDATE review_run SET status='failed', error=?,
                           finished_at=datetime('now') WHERE id IN ({marks})""",
                (error, *run_ids),
            ).rowcount
        wiki = conn.execute(
            f"""UPDATE wiki_page SET locked_by=NULL, locked_at=NULL,
                       owner_process_id=NULL, status='generating',
                       error='recovered from expired process lease'
                   WHERE status='generating' AND (({dead}) OR
                       (owner_process_id IS NULL AND locked_at <= datetime('now', ?)))""",
            (stale_arg,),
        ).rowcount
        conn.execute("DELETE FROM process_lease WHERE expires_at <= datetime('now')")
        conn.commit()
        return {"jobs": jobs, "runs": runs, "wiki": wiki}
    except sqlite3.Error:
        conn.rollback()
        raise
