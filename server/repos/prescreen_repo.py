def add(
    conn,
    *,
    pr_id,
    head_sha,
    model,
    complexity,
    score,
    reason,
    duration_ms,
    decided,
    diff_hash=None,
) -> int:
    cur = conn.execute(
        """INSERT INTO pre_screen
           (pr_id, head_sha, model, complexity, score, reason,
            duration_ms, decided, diff_hash, created_at)
           VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))""",
        (
            pr_id,
            head_sha,
            model,
            complexity,
            score,
            reason,
            duration_ms,
            decided,
            diff_hash,
        ),
    )
    conn.commit()
    return cur.lastrowid


def find_reusable(conn, pr_id, diff_hash, model):
    """같은 PR에서 동일 (diff 내용, model)로 이미 사전평가한 결과를 재사용해
    중복 CLI 호출을 피한다. prescreen 결정은 (diff, model)의 함수이므로 안전하며,
    diff_hash 키라 full/incremental diff 차이도 자동 구분된다."""
    return conn.execute(
        "SELECT * FROM pre_screen WHERE pr_id=? AND diff_hash=? AND model=? "
        "AND complexity IS NOT NULL ORDER BY id DESC LIMIT 1",
        (pr_id, diff_hash, model),
    ).fetchone()
