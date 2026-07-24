"""Bounded page queries for review-facing list APIs."""
from __future__ import annotations

_SEVERITY = "page_severity_order"
_CONSENSUS = "page_consensus_order"
_CONFIDENCE = "page_confidence_order"
_FILE = "page_file_order"
_FINDING_COLUMNS = """id,run_id,vendor_result_id,vendor,file,line,severity,category,
claim,rationale,confidence,consensus,consensus_group_id,status,edited_text,created_at,
posting_operation_id,source_chunk_index,owner_chunk_index,scope_status,posting_eligible,
duplicate_group_id,duplicate_suggested,verify_status,verify_rationale,
verify_independent,verify_evidence_status"""


def overview_snapshot_max(conn, *, pr_id=None) -> int:
    if pr_id is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(id),0) AS value FROM pull_request WHERE state='open'"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(MAX(id),0) AS value FROM pull_request WHERE id=?",
            (pr_id,),
        ).fetchone()
    return int(row["value"])


def overview_page(conn, *, snapshot_max_id, after, limit, pr_id=None):
    clauses = ["p.id<=?"]
    params = [snapshot_max_id]
    index_hint = ""
    if pr_id is not None:
        clauses.append("p.id=?")
        params.append(pr_id)
    else:
        clauses.append("p.state='open'")
        index_hint = "INDEXED BY idx_pull_request_overview_page"
        if after is not None:
            sort_at, row_id = after
            clauses.append("(p.overview_sort_at,p.id)<(?,?)")
            params.extend((sort_at, row_id))
    order_clause = (
        "" if pr_id is not None
        else "ORDER BY p.overview_sort_at DESC, p.id DESC"
    )
    return conn.execute(
        f"""SELECT p.id, p.number, p.title, r.full_name AS repo,
                    p.url, p.head_ref, p.head_sha, p.body,
                    p.author, p.created_at, p.first_seen_at, p.is_draft,
                    p.overview_sort_at,
                    (SELECT complexity FROM pre_screen ps
                       WHERE ps.pr_id=p.id AND ps.head_sha=p.head_sha
                       ORDER BY ps.id DESC LIMIT 1) AS prescreen,
                    (SELECT duration_ms FROM pre_screen ps
                       WHERE ps.pr_id=p.id AND ps.head_sha=p.head_sha
                       ORDER BY ps.id DESC LIMIT 1) AS prescreen_duration_ms,
                    (SELECT COUNT(*) FROM finding f
                       WHERE f.run_id=(SELECT id FROM review_run rr
                         WHERE rr.pr_id=p.id ORDER BY id DESC LIMIT 1)) AS finding_count,
                    (SELECT MIN(CASE f.severity WHEN 'critical' THEN 0 WHEN 'high'
                       THEN 1 WHEN 'medium' THEN 2 ELSE 3 END)
                       FROM finding f
                       WHERE f.run_id=(SELECT id FROM review_run rr
                         WHERE rr.pr_id=p.id ORDER BY id DESC LIMIT 1)) AS sev_rank,
                    (SELECT id FROM review_run rr WHERE rr.pr_id=p.id
                       ORDER BY id DESC LIMIT 1) AS run_id,
                    (SELECT head_sha FROM review_run rr WHERE rr.pr_id=p.id
                       ORDER BY id DESC LIMIT 1) AS run_head_sha,
                    (SELECT status FROM review_run rr WHERE rr.pr_id=p.id
                       ORDER BY id DESC LIMIT 1) AS run_status,
                    (SELECT started_at FROM review_run rr WHERE rr.pr_id=p.id
                       ORDER BY id DESC LIMIT 1) AS run_started_at,
                    (SELECT finished_at FROM review_run rr WHERE rr.pr_id=p.id
                       ORDER BY id DESC LIMIT 1) AS run_finished_at,
                    (SELECT CASE WHEN rr.started_at IS NULL THEN NULL
                              ELSE (strftime('%s', COALESCE(rr.finished_at, datetime('now'))) -
                                    strftime('%s', rr.started_at)) * 1000 END
                       FROM review_run rr WHERE rr.pr_id=p.id
                       ORDER BY id DESC LIMIT 1) AS run_duration_ms,
                    (SELECT error FROM review_run rr WHERE rr.pr_id=p.id
                       ORDER BY id DESC LIMIT 1) AS run_error,
                    (SELECT status FROM review_job j
                       WHERE j.pr_id=p.id AND j.head_sha=p.head_sha
                       ORDER BY id DESC LIMIT 1) AS job_status,
                    (SELECT error FROM review_job j
                       WHERE j.pr_id=p.id AND j.head_sha=p.head_sha
                       ORDER BY id DESC LIMIT 1) AS job_error,
                    (SELECT next_run_at FROM review_job j
                       WHERE j.pr_id=p.id AND j.head_sha=p.head_sha
                       ORDER BY id DESC LIMIT 1) AS job_next_run_at
             FROM pull_request p {index_hint}
             JOIN repo r ON r.id=p.repo_id
             WHERE {' AND '.join(clauses)}
             {order_clause}
             LIMIT ?""",
        (*params, limit),
    ).fetchall()


def runs_snapshot_max(conn, *, pr_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id),0) AS value FROM review_run WHERE pr_id=?",
        (pr_id,),
    ).fetchone()
    return int(row["value"])


def runs_page(conn, *, pr_id: int, snapshot_max_id: int, after_id, limit: int):
    clauses = ["r.pr_id=?", "r.id<=?"]
    params = [pr_id, snapshot_max_id]
    if after_id is not None:
        clauses.append("r.id<?")
        params.append(after_id)
    return conn.execute(
        f"""SELECT r.id, r.head_sha, r.trigger, r.status, r.error,
                  r.started_at, r.finished_at,
                  r.scope_requested_mode, r.scope_effective_mode,
                  r.scope_policy_reason, r.scope_selection_source,
                  r.dedupe_requested_mode, r.dedupe_effective_mode,
                  r.dedupe_policy_reason, r.dedupe_selection_source,
                  r.policy_cohort_key, r.policy_decision_hash,
                  r.policy_config_hash, r.benchmark_attestation_hash,
                  (SELECT COUNT(*) FROM finding f WHERE f.run_id=r.id) AS finding_count
           FROM review_run r INDEXED BY idx_review_run_page
           WHERE {' AND '.join(clauses)}
           ORDER BY r.id DESC LIMIT ?""",
        (*params, limit),
    ).fetchall()


def findings_snapshot(conn, *, run_id: int) -> tuple[int, dict]:
    row = conn.execute(
        "SELECT * FROM run_finding_summary WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        return 0, {"total_count": 0, "status_counts": {}, "postable_count": 0}
    status_counts = {
        status: int(row[f"{status}_count"])
        for status in ("pending", "approved", "dismissed", "edited", "posted", "unknown")
        if row[f"{status}_count"]
    }
    return int(row["max_finding_id"]), {
        "total_count": int(row["total_count"]),
        "status_counts": status_counts,
        "postable_count": int(row["postable_count"]),
    }


def finding_position(row) -> list:
    severity = {"critical": 0, "high": 1, "medium": 2}.get(row["severity"], 3)
    consensus = 0 if row["consensus"] == "consensus" else 1
    confidence = -(row["confidence"] if row["confidence"] is not None else -1)
    return [severity, consensus, confidence, row["file"] or "", int(row["id"])]


def findings_page(
    conn, *, run_id: int, snapshot_max_id: int, after, limit: int,
):
    clauses = ["run_id=?", "id<=?"]
    params = [run_id, snapshot_max_id]
    if after is not None:
        clauses.append(
            f"(({_SEVERITY}),({_CONSENSUS}),({_CONFIDENCE}),({_FILE}),id)"
            " > (?,?,?,?,?)"
        )
        params.extend(after)
    return conn.execute(
        f"""SELECT {_FINDING_COLUMNS} FROM finding INDEXED BY idx_finding_run_page_v3
           WHERE {' AND '.join(clauses)}
           ORDER BY {_SEVERITY}, {_CONSENSUS}, {_CONFIDENCE}, {_FILE}, id
           LIMIT ?""",
        (*params, limit),
    ).fetchall()
