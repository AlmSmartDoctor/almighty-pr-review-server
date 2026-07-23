import json


def add(
    conn,
    *,
    run_id,
    vendor,
    github_comment_id,
    url,
    marker,
    body,
    head_sha=None,
    finding_ids=None,
    kind="issue",
    commit=True,
) -> int:
    cur = conn.execute(
        """INSERT INTO posted_comment
           (run_id, vendor, github_comment_id, url, marker, body,
            head_sha, finding_ids, kind, posted_at)
           VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))""",
        (
            run_id,
            vendor,
            github_comment_id,
            url,
            marker,
            body,
            head_sha,
            json.dumps(finding_ids or []),
            kind,
        ),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def latest_for_pr_vendor(conn, *, pr_id, vendor):
    """같은 PR·벤더의 최신 비대체 코멘트(update-or-create 판단용)."""
    return conn.execute(
        """SELECT pc.* FROM posted_comment pc
           JOIN review_run rr ON rr.id = pc.run_id
           WHERE rr.pr_id=? AND pc.vendor=? AND pc.superseded_at IS NULL
           ORDER BY pc.id DESC LIMIT 1""",
        (pr_id, vendor),
    ).fetchone()


def supersede(conn, posted_id, *, commit=True):
    conn.execute(
        "UPDATE posted_comment SET superseded_at=datetime('now') WHERE id=?",
        (posted_id,),
    )
    if commit:
        conn.commit()
