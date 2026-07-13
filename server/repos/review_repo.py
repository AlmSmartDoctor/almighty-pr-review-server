import json


def create_run(conn, *, pr_id, head_sha, trigger, effort, merge_enabled=0) -> int:
    cur = conn.execute(
        """INSERT INTO review_run
           (pr_id, head_sha, trigger, effort, merge_enabled, status, started_at)
           VALUES (?,?,?,?,?, 'running', datetime('now'))""",
        (pr_id, head_sha, trigger, effort, merge_enabled),
    )
    conn.commit()
    return cur.lastrowid


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


def finish_run(conn, run_id, status, error=None):
    conn.execute(
        "UPDATE review_run SET status=?, error=?, finished_at=datetime('now') "
        "WHERE id=?",
        (status, error, run_id),
    )
    conn.commit()


def set_context(conn, run_id, *, text, meta):
    conn.execute(
        "UPDATE review_run SET context_text=?, context_meta=? WHERE id=?",
        (text, json.dumps(meta), run_id),
    )
    conn.commit()


def add_vendor_result(
    conn,
    *,
    run_id,
    vendor,
    status,
    duration_ms=None,
    tokens=None,
    raw_path=None,
    error=None,
) -> int:
    cur = conn.execute(
        """INSERT INTO vendor_result
           (run_id, vendor, status, duration_ms, tokens, raw_path, error)
           VALUES (?,?,?,?,?,?,?)""",
        (run_id, vendor, status, duration_ms, tokens, raw_path, error),
    )
    conn.commit()
    return cur.lastrowid


def get_run(conn, run_id):
    return conn.execute("SELECT * FROM review_run WHERE id=?", (run_id,)).fetchone()


def list_vendor_results(conn, run_id):
    # ★개정 (codex v6 [MEDIUM]): 부분 실패 벤더를 대시보드가 노출할 수 있게
    # run의 vendor_result 행을 반환(실패 벤더 배지 근거).
    return conn.execute(
        "SELECT vendor, status, error, duration_ms FROM vendor_result "
        "WHERE run_id=? ORDER BY vendor",
        (run_id,),
    ).fetchall()
