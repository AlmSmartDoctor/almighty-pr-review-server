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
