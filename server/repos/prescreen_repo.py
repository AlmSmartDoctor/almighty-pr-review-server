def add(
    conn, *, pr_id, head_sha, model, complexity, score, reason, duration_ms, decided
) -> int:
    cur = conn.execute(
        """INSERT INTO pre_screen
           (pr_id, head_sha, model, complexity, score, reason,
            duration_ms, decided, created_at)
           VALUES (?,?,?,?,?,?,?,?, datetime('now'))""",
        (pr_id, head_sha, model, complexity, score, reason, duration_ms, decided),
    )
    conn.commit()
    return cur.lastrowid


def latest_for_pr(conn, pr_id):
    return conn.execute(
        "SELECT * FROM pre_screen WHERE pr_id=? ORDER BY id DESC LIMIT 1",
        (pr_id,),
    ).fetchone()
