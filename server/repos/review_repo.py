import json

from server.review.finding_policy import policy_snapshot_record
from server.review.vendor_telemetry import (
    append_execution_attempt,
    validate_execution_envelope,
)


def create_run(
    conn, *, pr_id, head_sha, trigger, effort, merge_enabled=0,
    owner_process_id=None, owner_job_id=None, policy_snapshot=None
) -> int:
    policy = (
        policy_snapshot_record(policy_snapshot)
        if policy_snapshot is not None
        else {
            key: None
            for key in (
                "scope_requested_mode", "scope_effective_mode",
                "scope_policy_reason", "scope_selection_source",
                "dedupe_requested_mode", "dedupe_effective_mode",
                "dedupe_policy_reason", "dedupe_selection_source",
                "policy_cohort_key", "policy_decision_hash",
                "policy_config_hash", "benchmark_attestation_hash",
            )
        }
    )
    cur = conn.execute(
        """INSERT INTO review_run
           (pr_id, head_sha, trigger, effort, merge_enabled, status, started_at,
            owner_process_id, owner_job_id,
            scope_requested_mode, scope_effective_mode, scope_policy_reason,
            scope_selection_source, dedupe_requested_mode, dedupe_effective_mode,
            dedupe_policy_reason, dedupe_selection_source, policy_cohort_key,
            policy_decision_hash, policy_config_hash, benchmark_attestation_hash)
           VALUES (?,?,?,?,?, 'running', datetime('now'), ?, ?,
                   ?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            pr_id, head_sha, trigger, effort, merge_enabled,
            owner_process_id, owner_job_id,
            policy["scope_requested_mode"], policy["scope_effective_mode"],
            policy["scope_policy_reason"], policy["scope_selection_source"],
            policy["dedupe_requested_mode"], policy["dedupe_effective_mode"],
            policy["dedupe_policy_reason"], policy["dedupe_selection_source"],
            policy["policy_cohort_key"], policy["policy_decision_hash"],
            policy["policy_config_hash"], policy["benchmark_attestation_hash"],
        ),
    )
    conn.commit()
    return cur.lastrowid


def set_policy_snapshot(conn, run_id: int, policy_snapshot, *, commit=True) -> None:
    policy = policy_snapshot_record(policy_snapshot)
    cur = conn.execute(
        """UPDATE review_run SET
             scope_requested_mode=?, scope_effective_mode=?, scope_policy_reason=?,
             scope_selection_source=?, dedupe_requested_mode=?,
             dedupe_effective_mode=?, dedupe_policy_reason=?,
             dedupe_selection_source=?, policy_cohort_key=?,
             policy_decision_hash=?, policy_config_hash=?, benchmark_attestation_hash=?
           WHERE id=? AND status='running' AND policy_decision_hash IS NULL""",
        (
            policy["scope_requested_mode"], policy["scope_effective_mode"],
            policy["scope_policy_reason"], policy["scope_selection_source"],
            policy["dedupe_requested_mode"], policy["dedupe_effective_mode"],
            policy["dedupe_policy_reason"], policy["dedupe_selection_source"],
            policy["policy_cohort_key"], policy["policy_decision_hash"],
            policy["policy_config_hash"], policy["benchmark_attestation_hash"],
            run_id,
        ),
    )
    if cur.rowcount != 1:
        raise RuntimeError("run policy snapshot is not mutable")
    if commit:
        conn.commit()


def last_done_head_sha(conn, pr_id):
    """직전에 실제로 벤더 리뷰까지 완료(done)된 런의 head_sha. 증분 델타 기준선.
    prescreen auto-skip은 canceled로 마감되므로 done만 보면 '실제 리뷰된 sha'가 된다
    (last_reviewed_sha는 skip에도 전진하므로 기준선으로 부적합)."""
    row = conn.execute(
        "SELECT head_sha FROM review_run WHERE pr_id=? AND status='done' "
        "ORDER BY id DESC LIMIT 1",
        (pr_id,),
    ).fetchone()
    return row["head_sha"] if row else None


def set_base_sha(conn, run_id, base_sha):
    conn.execute("UPDATE review_run SET base_sha=? WHERE id=?", (base_sha, run_id))
    conn.commit()


def finish_run(conn, run_id, status, error=None, *, commit=True):
    conn.execute(
        "UPDATE review_run SET status=?, error=?, finished_at=datetime('now') "
        "WHERE id=?",
        (status, error, run_id),
    )
    if commit:
        conn.commit()


def recover_stale_running(conn) -> int:
    """부팅 시 이전 크래시/강제종료로 'running'에 고착된 run·vendor_result를 failed로
    마감한다(부팅 시점엔 실행 중인 리뷰가 있을 수 없다). 잡 복구(recover_stale)는
    review_job만 되살리므로, 짝이 되는 run을 정리하지 않으면 유령 'running' 행이
    영원히 duration 틱업하며 남는다."""
    error = "서버 재시작으로 중단됨"
    conn.execute(
        "UPDATE vendor_result SET status='failed', error=? WHERE status='running'",
        (error,),
    )
    cur = conn.execute(
        "UPDATE review_run SET status='failed', error=?, "
        "finished_at=datetime('now') WHERE status='running'",
        (error,),
    )
    conn.commit()
    return cur.rowcount


def set_context(conn, run_id, *, text, meta):
    conn.execute(
        "UPDATE review_run SET context_text=?, context_meta=? WHERE id=?",
        (text, json.dumps(meta), run_id),
    )
    conn.commit()


def _validate_context_chunks(chunks):
    if not isinstance(chunks, list) or len(chunks) > 1_000:
        raise ValueError("invalid context chunk list")
    total_text = 0
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) != {
            "chunk_hash", "context_hash", "text", "manifest"
        }:
            raise ValueError("invalid context chunk")
        for key in ("chunk_hash", "context_hash"):
            if not isinstance(chunk[key], str) or len(chunk[key]) != 64:
                raise ValueError("invalid context chunk hash")
        if not isinstance(chunk["text"], str):
            raise ValueError("invalid context chunk text")
        total_text += len(chunk["text"])
        if total_text > 2_000_000:
            raise ValueError("context chunks exceed size cap")
        if not isinstance(chunk["manifest"], list) or len(chunk["manifest"]) > 2_000:
            raise ValueError("invalid context chunk manifest")


def set_context_chunks(conn, run_id, *, chunks, meta_patch=None):
    _validate_context_chunks(chunks)
    row = conn.execute(
        "SELECT context_meta FROM review_run WHERE id=?", (run_id,)
    ).fetchone()
    decoded = json.loads(row["context_meta"] or "{}") if row else {}
    meta = decoded if isinstance(decoded, dict) else {}
    if meta_patch:
        meta.update(meta_patch)
    conn.execute(
        "UPDATE review_run SET context_chunks=?, context_meta=? WHERE id=?",
        (
            json.dumps(chunks, ensure_ascii=False, separators=(",", ":")),
            json.dumps(meta, ensure_ascii=False),
            run_id,
        ),
    )
    conn.commit()


def get_context_chunks(run):
    value = run["context_chunks"] if run is not None and "context_chunks" in run.keys() else None
    if not value:
        return None
    try:
        chunks = json.loads(value)
        _validate_context_chunks(chunks)
        return chunks
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def add_vendor_result(
    conn,
    *,
    run_id,
    vendor,
    status,
    duration_ms=None,
    raw_path=None,
    error=None,
) -> int:
    cur = conn.execute(
        """INSERT INTO vendor_result
           (run_id, vendor, status, duration_ms, raw_path, error, started_at)
           VALUES (?,?,?,?,?,?, datetime('now'))""",
        (run_id, vendor, status, duration_ms, raw_path, error),
    )
    conn.commit()
    return cur.lastrowid


def finish_vendor_result(
    conn,
    vr_id,
    *,
    status=None,
    error=None,
    duration_ms=None,
    raw_path=None,
    execution_meta=None,
    commit=True,
):
    """Persist only explicit terminal status and validated execution metadata.

    ``status`` remains optional for legacy callers: error implies failed, otherwise done.
    New retry attempts are appended instead of overwriting prior telemetry.
    """
    terminal = status or ("failed" if error is not None else "done")
    if terminal not in {"done", "partial", "failed", "timeout", "canceled"}:
        raise ValueError("invalid vendor result status")
    encoded = None
    if execution_meta is not None:
        validate_execution_envelope(execution_meta)
        row = conn.execute(
            "SELECT execution_meta FROM vendor_result WHERE id=?", (vr_id,)
        ).fetchone()
        if row is None:
            raise ValueError("vendor result not found")
        existing = _decode_execution_meta(row["execution_meta"])
        merged = append_execution_attempt(existing, execution_meta)
        encoded = json.dumps(merged, separators=(",", ":"), sort_keys=True)
    conn.execute(
        "UPDATE vendor_result SET status=?, error=?, duration_ms=?, "
        "raw_path=COALESCE(?, raw_path), "
        "execution_meta=COALESCE(?, execution_meta) WHERE id=?",
        (terminal, error, duration_ms, raw_path, encoded, vr_id),
    )
    if commit:
        conn.commit()


def get_run(conn, run_id):
    return conn.execute("SELECT * FROM review_run WHERE id=?", (run_id,)).fetchone()


def failed_vendors(conn, run_id):
    return [
        r["vendor"]
        for r in conn.execute(
            "SELECT vendor FROM vendor_result "
            "WHERE run_id=? AND status IN ('failed','partial','timeout')",
            (run_id,),
        ).fetchall()
    ]


def next_execution_attempt(conn, vr_ids) -> int:
    maximum = 0
    for vr_id in vr_ids:
        row = conn.execute(
            "SELECT execution_meta FROM vendor_result WHERE id=?", (vr_id,)
        ).fetchone()
        meta = _decode_execution_meta(row["execution_meta"] if row else None)
        if meta:
            maximum = max(
                maximum,
                *(attempt["attempt"] for attempt in meta["attempts"]),
            )
    return maximum + 1


def vendor_result_id(conn, *, run_id, vendor) -> int:
    """부분 재시도용: 기존 vendor_result 행 id를 반환(상태는 건드리지 않음)."""
    return conn.execute(
        "SELECT id FROM vendor_result WHERE run_id=? AND vendor=?", (run_id, vendor)
    ).fetchone()["id"]


def vendor_execution_meta(conn, *, run_id, vendor):
    row = conn.execute(
        "SELECT execution_meta FROM vendor_result WHERE run_id=? AND vendor=?",
        (run_id, vendor),
    ).fetchone()
    return _decode_execution_meta(row["execution_meta"] if row else None)


def _decode_execution_meta(value):
    if not value:
        return None
    try:
        decoded = json.loads(value)
        validate_execution_envelope(decoded)
        return decoded
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def list_vendor_results(conn, run_id):
    # raw_path is deliberately excluded from normal API/repository output.
    rows = conn.execute(
        """SELECT id, vendor, status, error, started_at, execution_meta,
                  CASE WHEN status='running' AND started_at IS NOT NULL
                       THEN (strftime('%s','now') - strftime('%s', started_at)) * 1000
                       ELSE duration_ms END AS duration_ms
           FROM vendor_result WHERE run_id=? ORDER BY vendor""",
        (run_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "vendor": row["vendor"],
            "status": row["status"],
            "error": row["error"],
            "started_at": row["started_at"],
            "duration_ms": row["duration_ms"],
            "execution_meta": _decode_execution_meta(row["execution_meta"]),
        }
        for row in rows
    ]
