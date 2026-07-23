import sqlite3


def add(conn: sqlite3.Connection, *, full_name: str, **overrides) -> int:
    if get_by_full_name(conn, full_name) is not None:
        raise sqlite3.IntegrityError("repo full_name already exists")
    cur = conn.execute("INSERT INTO repo (full_name) VALUES (?)", (full_name,))
    conn.commit()
    rid = cur.lastrowid
    if overrides:
        update(conn, rid, **overrides)
    return rid


def get(conn: sqlite3.Connection, rid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM repo WHERE id = ?", (rid,)).fetchone()


def get_by_full_name(conn: sqlite3.Connection, full_name: str) -> sqlite3.Row | None:
    # GitHub full_name은 대소문자 무시(정규 casing과 등록 casing이 달라도 매칭) → NOCASE.
    return conn.execute(
        "SELECT * FROM repo WHERE full_name = ? COLLATE NOCASE", (full_name,)
    ).fetchone()


def list_enabled(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM repo WHERE enabled = 1").fetchall()


ALLOWED = {
    "full_name",
    "enabled",
    "trigger_mode",
    "claude_model",
    "claude_effort",
    "codex_model",
    "codex_effort",
    "vendor_claude_on",
    "vendor_codex_on",
    "merge_enabled",
    "harness_name",
    "local_path",
    "last_polled_at",
    "last_poll_error",
    "context_static_on",
    "context_jira_on",
    "context_db_schema_on",
    "context_graphify_on",
    "context_feedback_on",
    "context_current_pr_reviews_on",
    "static_context_path",
    "jira_project_keys",
    "db_schema_path",
    "live_db_target_id",
    "graphify_path",
    "verify_singles_on",
    "incremental_review_on",
    "skip_draft_on",
    "review_scope_guard_mode",
    "review_dedupe_mode",
}


def has_active_work(conn: sqlite3.Connection, rid: int) -> bool:
    row = conn.execute(
        """SELECT EXISTS(
             SELECT 1 FROM review_job j
             JOIN pull_request p ON p.id=j.pr_id
             WHERE p.repo_id=? AND j.status IN ('queued', 'running')
           ) OR EXISTS(
             SELECT 1 FROM wiki_page w
             WHERE w.repo_id=? AND w.status='generating'
           ) AS active""",
        (rid, rid),
    ).fetchone()
    return bool(row["active"])


def remove(conn: sqlite3.Connection, rid: int) -> bool:
    """레포와 종속 리뷰 데이터를 FK 순서에 맞춰 한 transaction으로 제거한다."""
    if get(conn, rid) is None:
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        params = (rid,)
        run_ids = "SELECT rr.id FROM review_run rr JOIN pull_request p ON p.id=rr.pr_id WHERE p.repo_id=?"
        finding_ids = (
            "SELECT f.id FROM finding f JOIN review_run rr ON rr.id=f.run_id "
            "JOIN pull_request p ON p.id=rr.pr_id WHERE p.repo_id=?"
        )
        pr_ids = "SELECT id FROM pull_request WHERE repo_id=?"
        conn.execute(
            f"DELETE FROM finding_decision WHERE finding_id IN ({finding_ids})", params
        )
        for table in (
            "feedback_signal",
            "slack_post",
            "posted_comment",
            "finding",
            "vendor_result",
        ):
            conn.execute(f"DELETE FROM {table} WHERE run_id IN ({run_ids})", params)
        # finding.posting_operation_id points back to this table, so findings must be
        # removed first; the operation itself must be removed before review_run.
        conn.execute(
            f"DELETE FROM github_post_operation WHERE run_id IN ({run_ids})", params
        )
        conn.execute(f"DELETE FROM review_job WHERE pr_id IN ({pr_ids})", params)
        conn.execute(f"DELETE FROM review_run WHERE id IN ({run_ids})", params)
        conn.execute(f"DELETE FROM pre_screen WHERE pr_id IN ({pr_ids})", params)
        conn.execute("DELETE FROM pull_request WHERE repo_id=?", params)
        conn.execute("DELETE FROM wiki_page WHERE repo_id=?", params)
        conn.execute("DELETE FROM repo WHERE id=?", params)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def update(
    conn: sqlite3.Connection, rid: int, *, commit=True, **fields
) -> None:
    cols = [c for c in fields if c in ALLOWED]
    if not cols:
        return
    sets = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(
        f"UPDATE repo SET {sets} WHERE id = ?",
        [fields[c] for c in cols] + [rid],
    )
    if commit:
        conn.commit()
