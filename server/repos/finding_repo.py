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
    # 위험도(rank) → 벤더 합의 우선 → 신뢰도 내림차순. severity를 TEXT로 정렬하면
    # low가 medium 앞에 오는 역전 버그가 있어 CASE rank로 교정하고, 여러 벤더가
    # 합의한 지적과 고신뢰 지적을 상단에 노출한다(confidence 가중 우선순위화).
    return conn.execute(
        """SELECT * FROM finding WHERE run_id=?
           ORDER BY CASE severity
                      WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                      WHEN 'medium' THEN 2 ELSE 3 END,
                    CASE consensus WHEN 'consensus' THEN 0 ELSE 1 END,
                    confidence DESC, file""",
        (run_id,),
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
