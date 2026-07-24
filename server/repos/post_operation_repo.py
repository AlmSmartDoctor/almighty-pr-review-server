import hashlib
import json
import sqlite3


class PostingConflict(RuntimeError):
    pass


def _canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _operation_identity(*, repo_full: str, pr_number: int, head_sha: str, run_id: int,
                        vendor: str, marker_seed: str, body: str,
                        policy_review_identity: str) -> tuple[str, str, str, str]:
    """Return immutable key, marker, full body hash, and auditable identity JSON.

    The marker is derived before the final payload hash, avoiding a circular identity while
    binding every replay to canonical repo/PR/head/run/vendor/policy and the exact body.
    """
    core = {
        "canonical_repo": repo_full.strip().lower(), "pr_number": pr_number,
        "head_sha": head_sha, "run_id": run_id, "vendor": vendor,
        "policy_review_identity": policy_review_identity or "unknown",
        "marker_seed": marker_seed,
    }
    marker = "<!-- almighty-post-operation:" + hashlib.sha256(_canonical_json(core).encode()).hexdigest()[:32] + " -->"
    full_body = f"{body}\n\n{marker}"
    body_hash = hashlib.sha256(full_body.encode()).hexdigest()
    identity = {**core, "marker": marker, "full_body_hash": body_hash}
    identity_json = _canonical_json(identity)
    key = hashlib.sha256(identity_json.encode()).hexdigest()
    return key, marker, body_hash, identity_json


def prepare(conn, *, run_id: int, vendor: str, body: str, all_ids: list[int], new_ids: list[int],
            repo_full: str = "", pr_number: int = 0, head_sha: str = "",
            policy_review_identity: str = "unknown"):
    """Atomically reserve findings under an exact operation identity.

    Optional target fields retain compatibility for legacy direct callers; the API always
    supplies the canonical target and immutable run policy identity.
    """
    key, marker, body_hash, identity_json = _operation_identity(
        repo_full=repo_full, pr_number=pr_number, head_sha=head_sha, run_id=run_id,
        vendor=vendor, marker_seed=hashlib.sha256(body.encode()).hexdigest(),
        body=body, policy_review_identity=policy_review_identity,
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM github_post_operation WHERE operation_key=?", (key,)
        ).fetchone()
        if existing is not None:
            conn.commit()
            return existing
        cur = conn.execute(
            """INSERT INTO github_post_operation
               (operation_key, run_id, vendor, marker, body, body_hash, identity_json, finding_ids,
                new_finding_ids, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?, 'pending', datetime('now'), datetime('now'))""",
            (key, run_id, vendor, marker, body, body_hash, identity_json,
             json.dumps(all_ids), json.dumps(new_ids)),
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
        return conn.execute("SELECT * FROM github_post_operation WHERE id=?", (operation_id,)).fetchone()
    except Exception:
        conn.rollback()
        raise


def succeeded_for_run(conn, run_id: int):
    return conn.execute(
        """SELECT * FROM github_post_operation
           WHERE run_id=? AND status='succeeded'
             AND body_hash IS NOT NULL AND identity_json IS NOT NULL
           ORDER BY id""",
        (run_id,),
    ).fetchall()


def has_legacy_identity(conn, run_id: int) -> bool:
    return conn.execute(
        """SELECT 1 FROM github_post_operation
           WHERE run_id=? AND (body_hash IS NULL OR identity_json IS NULL)
           LIMIT 1""",
        (run_id,),
    ).fetchone() is not None


def claim(conn, operation_id: int, owner_token: str) -> bool:
    cur = conn.execute(
        """UPDATE github_post_operation SET status='applying', owner_token=?,
                  locked_at=datetime('now'), updated_at=datetime('now')
           WHERE id=? AND (status='pending' OR
                 (status='applying' AND locked_at <= datetime('now', '-20 minutes')))""",
        (owner_token, operation_id),
    )
    conn.commit()
    return cur.rowcount == 1


def abort_unapplied(conn, operation_id: int, *, owner_token: str) -> None:
    op = conn.execute(
        "SELECT * FROM github_post_operation WHERE id=?", (operation_id,)
    ).fetchone()
    if (
        op is None or op["status"] != "applying"
        or op["owner_token"] != owner_token or op["remote_review_id"]
    ):
        raise PostingConflict("posting operation cannot be safely aborted")
    conn.execute(
        "UPDATE finding SET posting_operation_id=NULL WHERE posting_operation_id=?",
        (operation_id,),
    )
    conn.execute("DELETE FROM github_post_operation WHERE id=?", (operation_id,))
    conn.commit()


def mark_remote(conn, operation_id: int, *, review_id: str, url: str, owner_token: str) -> None:
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
           WHERE id=? AND status='applying' AND owner_token=?""", (error[:1000], operation_id, owner_token)
    )
    conn.commit()


def finalize(conn, operation_id: int, *, posted_id: int | None = None) -> None:
    op = conn.execute("SELECT * FROM github_post_operation WHERE id=?", (operation_id,)).fetchone()
    new_ids = json.loads(op["new_finding_ids"] or "[]")
    for fid in new_ids:
        cur = conn.execute(
            """UPDATE finding SET status='posted', posting_operation_id=NULL
               WHERE id=? AND posting_operation_id=? AND status IN ('approved','edited')""", (fid, operation_id)
        )
        if cur.rowcount != 1:
            raise PostingConflict(f"finding {fid} posting reservation lost")
    conn.execute(
        """UPDATE github_post_operation SET status='succeeded', error=NULL,
                  owner_token=NULL, locked_at=NULL, completed_at=datetime('now'),
                  updated_at=datetime('now') WHERE id=?""", (operation_id,)
    )


def find_remote_review(gh, repo: str, number: int, marker: str):
    list_reviews = getattr(gh, "list_pr_reviews_complete", None)
    if not callable(list_reviews):
        list_reviews = getattr(gh, "list_pr_reviews", None)
    if not callable(list_reviews):
        return None
    for review in reversed(list_reviews(repo, number)):
        if marker in (review.get("body") or ""):
            return {"id": review.get("id"), "html_url": review.get("html_url") or review.get("url") or ""}
    return None
