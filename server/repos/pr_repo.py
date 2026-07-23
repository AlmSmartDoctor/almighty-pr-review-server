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
    base_sha="",
    state="open",
    created_at=None,
    head_ref="",
    body="",
    is_draft=False,
    commit=True,
) -> int:
    conn.execute(
        """INSERT INTO pull_request
           (repo_id, number, title, author, head_sha, base_ref, base_sha, state, url,
            created_at, head_ref, body, is_draft, first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))
           ON CONFLICT(repo_id, number) DO UPDATE SET
             title=excluded.title, author=excluded.author,
             head_sha=excluded.head_sha, base_ref=excluded.base_ref,
             base_sha=excluded.base_sha, state=excluded.state, url=excluded.url,
             created_at=COALESCE(excluded.created_at, pull_request.created_at),
             head_ref=excluded.head_ref, body=excluded.body,
             is_draft=excluded.is_draft,
             updated_at=datetime('now')
           WHERE pull_request.title IS NOT excluded.title
              OR pull_request.author IS NOT excluded.author
              OR pull_request.head_sha IS NOT excluded.head_sha
              OR pull_request.base_ref IS NOT excluded.base_ref
              OR pull_request.base_sha IS NOT excluded.base_sha
              OR pull_request.state IS NOT excluded.state
              OR pull_request.url IS NOT excluded.url
              OR pull_request.head_ref IS NOT excluded.head_ref
              OR pull_request.body IS NOT excluded.body
              OR pull_request.is_draft IS NOT excluded.is_draft""",
        (
            repo_id,
            number,
            title,
            author,
            head_sha,
            base_ref,
            base_sha,
            state,
            url,
            created_at,
            head_ref,
            body,
            1 if is_draft else 0,
        ),
    )
    if commit:
        conn.commit()
    return conn.execute(
        "SELECT id FROM pull_request WHERE repo_id=? AND number=?",
        (repo_id, number),
    ).fetchone()["id"]


def get(conn, pid) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pull_request WHERE id = ?", (pid,)).fetchone()


def mark_reviewed(conn, pid, head_sha, *, commit=True) -> None:
    conn.execute(
        "UPDATE pull_request SET last_reviewed_sha=? WHERE id=?",
        (head_sha, pid),
    )
    if commit:
        conn.commit()


def needs_review(conn, pid) -> bool:
    r = get(conn, pid)
    return r is not None and r["head_sha"] != r["last_reviewed_sha"]


def mark_closed(conn, repo_id, numbers, *, commit=True) -> int:
    """주어진 PR 번호들 중 'open' 행을 'closed'로 재조정. 호출자는 '이 폴 이전에 열려
    있었으나 gh 오픈 목록에서 사라진' 번호만 넘긴다(폴 도중 삽입된 PR은 애초에 집합에
    없어 오검-close 방지). SQLite 변수 상한(999) 회피로 청크 처리."""
    numbers = list(numbers)
    total = 0
    for i in range(0, len(numbers), 500):
        chunk = numbers[i : i + 500]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"UPDATE pull_request SET state='closed' "
            f"WHERE repo_id=? AND state='open' AND number IN ({placeholders})",
            (repo_id, *chunk),
        )
        total += cur.rowcount
    if total and commit:
        conn.commit()
    return total
