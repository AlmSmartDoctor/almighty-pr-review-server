def add(
    conn,
    *,
    run_id,
    vendor,
    file,
    line,
    severity,
    category,
    claim,
    rationale,
    confidence,
    vendor_result_id=None,
    consensus="single",
    consensus_group_id=None,
    verify_status=None,
    verify_rationale=None,
) -> int:
    cur = conn.execute(
        """INSERT INTO finding
           (run_id, vendor_result_id, vendor, file, line, severity, category,
            claim, rationale, confidence, consensus, consensus_group_id,
            verify_status, verify_rationale, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))""",
        (
            run_id,
            vendor_result_id,
            vendor,
            file,
            line,
            severity,
            category,
            claim,
            rationale,
            confidence,
            consensus,
            consensus_group_id,
            verify_status,
            verify_rationale,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get(conn, fid):
    return conn.execute("SELECT * FROM finding WHERE id=?", (fid,)).fetchone()


def list_for_run(conn, run_id):
    return conn.execute(
        "SELECT * FROM finding WHERE run_id=? ORDER BY severity, file", (run_id,)
    ).fetchall()


_UNSET = object()


def set_status(conn, fid, status, edited_text=_UNSET):
    prev = conn.execute("SELECT status FROM finding WHERE id=?", (fid,)).fetchone()
    if edited_text is _UNSET:
        conn.execute("UPDATE finding SET status=? WHERE id=?", (status, fid))
    else:
        conn.execute(
            "UPDATE finding SET status=?, edited_text=? WHERE id=?",
            (status, edited_text, fid),
        )
    # 상태가 실제로 바뀔 때만 append-only 감사 행 기록(재편집 등 무변경·미존재 finding은 스킵).
    if prev is not None and prev["status"] != status:
        conn.execute(
            "INSERT INTO finding_decision (finding_id, from_status, to_status, decided_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (fid, prev["status"], status),
        )
    conn.commit()
