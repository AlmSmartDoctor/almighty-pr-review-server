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
) -> int:
    cur = conn.execute(
        """INSERT INTO finding
           (run_id, vendor_result_id, vendor, file, line, severity, category,
            claim, rationale, confidence, consensus, consensus_group_id,
            created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))""",
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
    if edited_text is _UNSET:
        conn.execute("UPDATE finding SET status=? WHERE id=?", (status, fid))
    else:
        conn.execute(
            "UPDATE finding SET status=?, edited_text=? WHERE id=?",
            (status, edited_text, fid),
        )
    conn.commit()
