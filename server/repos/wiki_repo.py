"""Persistence for the latest per-repository Ground Truth Wiki snapshot."""

import json


GENERATION_STALE_MINUTES = 30
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
    }


def list_pages(conn) -> list[dict]:
    recover_stale_generations(conn)
    rows = conn.execute(
        """SELECT r.id AS repo_id, r.full_name AS repo,
                  w.status, w.content, w.sources, w.source_sha,
                  w.generated_at, w.error
           FROM repo r
           LEFT JOIN wiki_page w ON w.repo_id = r.id
           ORDER BY r.full_name COLLATE NOCASE"""
    ).fetchall()
    return [_row(row) for row in rows]


def get_page(conn, repo_id: int):
    row = conn.execute(
        """SELECT r.id AS repo_id, r.full_name AS repo,
                  w.status, w.content, w.sources, w.source_sha,
                  w.generated_at, w.error
           FROM repo r
           LEFT JOIN wiki_page w ON w.repo_id = r.id
           WHERE r.id=?""",
        (repo_id,),
    ).fetchone()
    return _row(row) if row else None


def recover_stale_generations(conn) -> int:
    """프로세스 종료 등으로 남은 generating 잠금을 실패 상태로 복구한다."""
    cur = conn.execute(
        """UPDATE wiki_page
           SET status='failed', error=?, updated_at=datetime('now')
           WHERE status='generating'
             AND updated_at <= datetime('now', ?)""",
        (_STALE_ERROR, _stale_modifier()),
    )
    if cur.rowcount:
        conn.commit()
    return cur.rowcount


def mark_generating(conn, repo_id: int) -> bool:
    """새 생성 잠금을 잡는다. 제한 시간을 넘긴 잠금은 한 번의 원자 연산으로 탈취한다."""
    cur = conn.execute(
        """INSERT INTO wiki_page (repo_id, status, updated_at)
           VALUES (?, 'generating', datetime('now'))
           ON CONFLICT(repo_id) DO UPDATE SET
             status='generating', error=NULL, updated_at=datetime('now')
           WHERE wiki_page.status <> 'generating'
              OR wiki_page.updated_at <= datetime('now', ?)""",
        (repo_id, _stale_modifier()),
    )
    conn.commit()
    return cur.rowcount == 1


def save(conn, repo_id: int, *, page: dict, sources: list, source_sha: str) -> None:
    conn.execute(
        """INSERT INTO wiki_page
             (repo_id, status, content, sources, source_sha, generated_at, error, updated_at)
           VALUES (?, 'ready', ?, ?, ?, datetime('now'), NULL, datetime('now'))
           ON CONFLICT(repo_id) DO UPDATE SET
             status='ready', content=excluded.content, sources=excluded.sources,
             source_sha=excluded.source_sha, generated_at=excluded.generated_at,
             error=NULL, updated_at=excluded.updated_at""",
        (
            repo_id,
            json.dumps(page, ensure_ascii=False),
            json.dumps(sources, ensure_ascii=False),
            source_sha,
        ),
    )
    conn.commit()


def mark_failed(conn, repo_id: int, error: str) -> None:
    conn.execute(
        """INSERT INTO wiki_page (repo_id, status, error, updated_at)
           VALUES (?, 'failed', ?, datetime('now'))
           ON CONFLICT(repo_id) DO UPDATE SET
             status='failed', error=excluded.error, updated_at=excluded.updated_at""",
        (repo_id, error[:1000]),
    )
    conn.commit()
