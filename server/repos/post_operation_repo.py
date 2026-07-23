import hashlib
import json
import sqlite3


class PostingConflict(RuntimeError):
    pass


def _key(run_id: int, vendor: str, body: str) -> str:
    digest = hashlib.sha256(body.encode()).hexdigest()[:16]
    return f"{run_id}:{vendor}:{digest}"


def prepare(conn, *, run_id: int, vendor: str, body: str, all_ids: list[int], new_ids: list[int]):
    """게시 snapshot과 finding 예약을 원자 생성한다. 기존 pending operation은 재사용한다."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        key = _key(run_id, vendor, body)
        existing = conn.execute(
            "SELECT * FROM github_post_operation WHERE operation_key=?",
            (key,),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return existing
        marker = f"<!-- almighty-post-operation:{key} -->"
        cur = conn.execute(
            """INSERT INTO github_post_operation
               (operation_key, run_id, vendor, marker, body, finding_ids,
                new_finding_ids, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?, 'pending', datetime('now'), datetime('now'))""",
            (key, run_id, vendor, marker, body, json.dumps(all_ids), json.dumps(new_ids)),
        )
        operation_id = cur.lastrowid
        for fid in new_ids:
            reserved = conn.execute(
                """UPDATE finding SET posting_operation_id=?
                   WHERE id=? AND run_id=? AND status IN ('approved','edited')
                     AND posting_operation_id IS NULL""",
                (operation_id, fid, run_id),
            )
            if reserved.rowcount != 1:
                raise PostingConflict(f"finding {fid} changed while preparing post")
        conn.commit()
        return conn.execute(
            "SELECT * FROM github_post_operation WHERE id=?", (operation_id,)
        ).fetchone()
    except Exception:
        conn.rollback()
        raise


def claim(conn, operation_id: int, owner_token: str) -> bool:
    # update 404→create까지 GitHub timeout 두 번(최대 약 10분)이 가능하므로 정상 owner를
    # 회수하지 않도록 20분 뒤에만 stale reclaim한다.
    cur = conn.execute(
        """UPDATE github_post_operation SET status='applying', owner_token=?,
                  locked_at=datetime('now'), updated_at=datetime('now')
           WHERE id=? AND (status='pending' OR
                 (status='applying' AND locked_at <= datetime('now', '-20 minutes')))""",
        (owner_token, operation_id),
    )
    conn.commit()
    return cur.rowcount == 1


def mark_remote(
    conn, operation_id: int, *, review_id: str, url: str, owner_token: str
) -> None:
    cur = conn.execute(
        """UPDATE github_post_operation SET status='remote_applied',
                  remote_review_id=?, remote_url=?, error=NULL, owner_token=NULL,
                  locked_at=NULL, updated_at=datetime('now')
           WHERE id=? AND status='applying' AND owner_token=?""",
        (review_id, url, operation_id, owner_token),
    )
    if cur.rowcount != 1:
        conn.rollback()
        raise PostingConflict("post operation ownership lost")
    conn.commit()


def mark_error(conn, operation_id: int, error: str, *, owner_token: str) -> None:
    conn.execute(
        """UPDATE github_post_operation SET error=?, updated_at=datetime('now')
           WHERE id=? AND status='applying' AND owner_token=?""",
        (error[:1000], operation_id, owner_token),
    )
    conn.commit()


def finalize(conn, operation_id: int, *, posted_id: int | None = None) -> None:
    op = conn.execute(
        "SELECT * FROM github_post_operation WHERE id=?", (operation_id,)
    ).fetchone()
    new_ids = json.loads(op["new_finding_ids"] or "[]")
    for fid in new_ids:
        cur = conn.execute(
            """UPDATE finding SET status='posted', posting_operation_id=NULL
               WHERE id=? AND posting_operation_id=? AND status IN ('approved','edited')""",
            (fid, operation_id),
        )
        if cur.rowcount != 1:
            raise PostingConflict(f"finding {fid} posting reservation lost")
    conn.execute(
        """UPDATE github_post_operation SET status='succeeded', error=NULL,
                  owner_token=NULL, locked_at=NULL,
                  completed_at=datetime('now'), updated_at=datetime('now') WHERE id=?""",
        (operation_id,),
    )


def find_remote_review(gh, repo: str, number: int, marker: str):
    list_reviews = getattr(gh, "list_pr_reviews", None)
    if not callable(list_reviews):
        return None
    for review in reversed(list_reviews(repo, number)):
        if marker in (review.get("body") or ""):
            return {
                "id": review.get("id"),
                "html_url": review.get("html_url") or review.get("url") or "",
            }
    return None
