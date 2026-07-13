import sqlite3


def upsert(
    conn,
    *,
    repo_id,
    number,
    title,
    author,
    head_sha,
    base_ref,
    url,
    state="open",
    created_at=None,
    head_ref="",
    body="",
) -> int:
    conn.execute(
        """INSERT INTO pull_request
           (repo_id, number, title, author, head_sha, base_ref, state, url,
            created_at, head_ref, body, first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))
           ON CONFLICT(repo_id, number) DO UPDATE SET
             title=excluded.title, author=excluded.author,
             head_sha=excluded.head_sha, base_ref=excluded.base_ref,
             state=excluded.state, url=excluded.url,
             created_at=COALESCE(excluded.created_at, pull_request.created_at),
             head_ref=excluded.head_ref, body=excluded.body,
             updated_at=datetime('now')""",
        (
            repo_id,
            number,
            title,
            author,
            head_sha,
            base_ref,
            state,
            url,
            created_at,
            head_ref,
            body,
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM pull_request WHERE repo_id=? AND number=?",
        (repo_id, number),
    ).fetchone()["id"]


def get(conn, pid) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pull_request WHERE id = ?", (pid,)).fetchone()


def mark_reviewed(conn, pid, head_sha) -> None:
    conn.execute(
        "UPDATE pull_request SET last_reviewed_sha=? WHERE id=?",
        (head_sha, pid),
    )
    conn.commit()


def needs_review(conn, pid) -> bool:
    r = get(conn, pid)
    return r is not None and r["head_sha"] != r["last_reviewed_sha"]
