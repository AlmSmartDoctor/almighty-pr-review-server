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


def finish_vendor_result(conn, vr_id, *, error=None, duration_ms=None, raw_path=None):
    """벤더 실행 종료 반영: error가 있으면 failed, 없으면 done(+error=NULL —
    부분 재시도 성공 시 이전 실패 흔적을 지운다; 첫 실행에선 이미 NULL이라 no-op).
    raw_path는 값이 있을 때만 갱신(COALESCE — 저장 실패로 이전 원문을 지우지 않음)."""
    if error is not None:
        conn.execute(
            "UPDATE vendor_result SET status='failed', error=?, duration_ms=?, "
            "raw_path=COALESCE(?, raw_path) WHERE id=?",
            (error, duration_ms, raw_path, vr_id),
        )
    else:
        conn.execute(
            "UPDATE vendor_result SET status='done', error=NULL, duration_ms=?, "
            "raw_path=COALESCE(?, raw_path) WHERE id=?",
            (duration_ms, raw_path, vr_id),
        )
    conn.commit()


def get_run(conn, run_id):
    return conn.execute("SELECT * FROM review_run WHERE id=?", (run_id,)).fetchone()


def failed_vendors(conn, run_id):
    return [
        r["vendor"]
        for r in conn.execute(
            "SELECT vendor FROM vendor_result WHERE run_id=? AND status='failed'",
            (run_id,),
        ).fetchall()
    ]


def vendor_result_id(conn, *, run_id, vendor) -> int:
    """부분 재시도용: 기존 vendor_result 행 id를 반환(상태는 건드리지 않음).
    'running'으로 미리 바꾸지 않으므로 재시도 도중 크래시해도 행은 'failed'로 남아
    다음 재시도가 self-heal한다(run당 벤더 1행 불변식 유지, 새 행 미생성)."""
    return conn.execute(
        "SELECT id FROM vendor_result WHERE run_id=? AND vendor=?", (run_id, vendor)
    ).fetchone()["id"]


def list_vendor_results(conn, run_id):
    # ★개정 (codex v6 [MEDIUM]): 부분 실패 벤더를 대시보드가 노출할 수 있게
    # run의 vendor_result 행을 반환(실패 벤더 배지 근거).
    # running 벤더는 아직 duration_ms가 없으므로 서버가 경과시간을 실시간 계산해
    # 반환한다(run_duration_ms와 동일 공식) → 상세 트레이스가 폴링만으로 틱업.
    return conn.execute(
        """SELECT id, vendor, status, error, started_at, raw_path,
                  CASE WHEN status='running' AND started_at IS NOT NULL
                       THEN (strftime('%s','now') - strftime('%s', started_at)) * 1000
                       ELSE duration_ms END AS duration_ms
           FROM vendor_result WHERE run_id=? ORDER BY vendor""",
        (run_id,),
    ).fetchall()
