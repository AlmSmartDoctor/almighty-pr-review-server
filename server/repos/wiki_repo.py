"""Persistence for the latest per-repository Ground Truth Wiki snapshot."""

import json
import sqlite3


GENERATION_STALE_MINUTES = 30
DEFAULT_MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 120
_STALE_ERROR = "이전 Wiki 생성 작업이 제한 시간을 초과해 종료된 것으로 처리되었습니다"


def _stale_modifier() -> str:
    return f"-{GENERATION_STALE_MINUTES} minutes"


def _json(raw, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row(row) -> dict:
    status = row["status"] or "empty"
    return {
        "repo_id": row["repo_id"],
        "repo": row["repo"],
        "status": status,
        "page": _json(row["content"], None),
        "sources": _json(row["sources"], []),
        "source_sha": row["source_sha"],
        "generated_at": row["generated_at"],
        "error": row["error"],
        "attempts": row["attempts"] or 0,
        "max_attempts": row["max_attempts"] or DEFAULT_MAX_ATTEMPTS,
        "next_run_at": row["next_run_at"],
    }


def list_pages(conn) -> list[dict]:
    recover_stale_generations(conn)
    rows = conn.execute(
        """SELECT r.id AS repo_id, r.full_name AS repo,
                  w.status, w.content, w.sources, w.source_sha,
                  w.generated_at, w.error, w.attempts, w.max_attempts,
                  w.next_run_at
           FROM repo r
           LEFT JOIN wiki_page w ON w.repo_id = r.id
           ORDER BY r.full_name COLLATE NOCASE"""
    ).fetchall()
    return [_row(row) for row in rows]


def get_page(conn, repo_id: int):
    row = conn.execute(
        """SELECT r.id AS repo_id, r.full_name AS repo,
                  w.status, w.content, w.sources, w.source_sha,
                  w.generated_at, w.error, w.attempts, w.max_attempts,
                  w.next_run_at
           FROM repo r
           LEFT JOIN wiki_page w ON w.repo_id = r.id
           WHERE r.id=?""",
        (repo_id,),
    ).fetchone()
    return _row(row) if row else None


def recover_stale_generations(conn) -> int:
    """오래 대기했지만 worker가 claim하지 못한 생성 요청을 실패로 복구한다."""
    cur = conn.execute(
        """UPDATE wiki_page
           SET status='failed', error=?, next_run_at=NULL,
               updated_at=datetime('now')
           WHERE status='generating' AND locked_at IS NULL
             AND updated_at <= datetime('now', ?)""",
        (_STALE_ERROR, _stale_modifier()),
    )
    if cur.rowcount:
        conn.commit()
    return cur.rowcount


def recover_running(conn) -> int:
    """서버 재시작 시 이전 프로세스가 claim한 Wiki 요청을 다시 대기열로 돌린다."""
    cur = conn.execute(
        """UPDATE wiki_page
           SET locked_by=NULL, locked_at=NULL, error=NULL, updated_at=datetime('now')
           WHERE status='generating' AND locked_at IS NOT NULL"""
    )
    if cur.rowcount:
        conn.commit()
    return cur.rowcount


def mark_generating(conn, repo_id: int) -> bool:
    """생성 요청을 등록한다. 실행 중 요청은 유지하고 오래된 미claim 요청만 교체한다."""
    cur = conn.execute(
        """INSERT INTO wiki_page
             (repo_id, status, attempts, max_attempts, next_run_at,
              locked_by, locked_at, error, updated_at)
           VALUES (?, 'generating', 0, ?, NULL, NULL, NULL, NULL, datetime('now'))
           ON CONFLICT(repo_id) DO UPDATE SET
             status='generating', attempts=0, max_attempts=excluded.max_attempts,
             next_run_at=NULL, locked_by=NULL, locked_at=NULL,
             error=NULL, updated_at=datetime('now')
           WHERE wiki_page.status <> 'generating'
              OR (wiki_page.locked_at IS NULL
                  AND wiki_page.updated_at <= datetime('now', ?))""",
        (repo_id, DEFAULT_MAX_ATTEMPTS, _stale_modifier()),
    )
    conn.commit()
    return cur.rowcount == 1


def claim_next(conn, *, worker_id: str, owner_process_id=None):
    """가장 오래된 미claim Wiki 생성 요청 하나를 원자적으로 선점한다."""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return None
    try:
        row = conn.execute(
            """SELECT repo_id FROM wiki_page
               WHERE status='generating' AND locked_at IS NULL
                 AND (next_run_at IS NULL OR next_run_at <= datetime('now'))
               ORDER BY COALESCE(next_run_at, updated_at), repo_id LIMIT 1"""
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            """UPDATE wiki_page
               SET locked_by=?, locked_at=datetime('now'), owner_process_id=?,
                   attempts=attempts+1, next_run_at=NULL
               WHERE repo_id=? AND status='generating' AND locked_at IS NULL""",
            (worker_id, owner_process_id, row["repo_id"]),
        )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
        return None
    return row["repo_id"]


def save(
    conn, repo_id: int, *, page: dict, sources: list, source_sha: str,
    owner_process_id=None
) -> None:
    cur = conn.execute(
        """INSERT INTO wiki_page
             (repo_id, status, content, sources, source_sha, generated_at,
              error, owner_process_id, updated_at)
           VALUES (?, 'ready', ?, ?, ?, datetime('now'), NULL, NULL, datetime('now'))
           ON CONFLICT(repo_id) DO UPDATE SET
             status='ready', content=excluded.content, sources=excluded.sources,
             source_sha=excluded.source_sha, generated_at=excluded.generated_at,
             error=NULL, next_run_at=NULL, locked_by=NULL, locked_at=NULL,
             owner_process_id=NULL, updated_at=excluded.updated_at
           WHERE ? IS NULL OR wiki_page.owner_process_id=?""",
        (
            repo_id,
            json.dumps(page, ensure_ascii=False),
            json.dumps(sources, ensure_ascii=False),
            source_sha,
            owner_process_id,
            owner_process_id,
        ),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"wiki {repo_id} process lease lost")
    conn.commit()


def schedule_retry(
    conn, repo_id: int, error: str, *, base_seconds: int = RETRY_BASE_SECONDS,
    owner_process_id=None
) -> bool:
    """현재 attempt가 남아 있으면 지수 backoff로 다시 대기시키고 True를 반환한다."""
    row = conn.execute(
        "SELECT attempts, max_attempts FROM wiki_page WHERE repo_id=?", (repo_id,)
    ).fetchone()
    if row is None or row["attempts"] >= row["max_attempts"]:
        return False
    delay = base_seconds * (2 ** max(row["attempts"] - 1, 0))
    cur = conn.execute(
        """UPDATE wiki_page
           SET status='generating', error=?, next_run_at=datetime('now', ?),
               locked_by=NULL, locked_at=NULL, owner_process_id=NULL,
               updated_at=datetime('now')
           WHERE repo_id=? AND status='generating'
             AND (? IS NULL OR owner_process_id=?)""",
        (error[:1000], f"+{delay} seconds", repo_id,
         owner_process_id, owner_process_id),
    )
    conn.commit()
    return cur.rowcount == 1


def mark_failed(conn, repo_id: int, error: str, *, owner_process_id=None) -> None:
    conn.execute(
        """INSERT INTO wiki_page (repo_id, status, error, updated_at)
           VALUES (?, 'failed', ?, datetime('now'))
           ON CONFLICT(repo_id) DO UPDATE SET
             status='failed', error=excluded.error, next_run_at=NULL,
             locked_by=NULL, locked_at=NULL, owner_process_id=NULL,
             updated_at=excluded.updated_at
           WHERE ? IS NULL OR wiki_page.owner_process_id=?""",
        (repo_id, error[:1000], owner_process_id, owner_process_id),
    )
    conn.commit()
