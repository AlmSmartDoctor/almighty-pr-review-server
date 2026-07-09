import sqlite3


def enqueue(conn, *, pr_id, head_sha, trigger, priority=0) -> int:
    conn.execute(
        """INSERT INTO review_job (pr_id, head_sha, trigger, priority, created_at)
           VALUES (?,?,?,?, datetime('now'))
           ON CONFLICT(pr_id, head_sha) DO NOTHING""",
        (pr_id, head_sha, trigger, priority),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM review_job WHERE pr_id=? AND head_sha=?",
        (pr_id, head_sha),
    ).fetchone()["id"]


def enqueue_manual(conn, *, pr_id, head_sha, priority=0) -> int:
    """수동 트리거: 같은 (pr, sha)에 종료된(done/failed/canceled) 잡이 있으면
    queued로 재개해 재리뷰가 실제로 재실행되게 한다. 진행 중(queued/running)이면
    건드리지 않고 id만 반환. (auto 폴링의 enqueue는 DO NOTHING 멱등성 유지.)"""
    row = conn.execute(
        "SELECT id, status FROM review_job WHERE pr_id=? AND head_sha=?",
        (pr_id, head_sha),
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO review_job (pr_id, head_sha, trigger, priority, created_at)
               VALUES (?,?, 'manual', ?, datetime('now'))""",
            (pr_id, head_sha, priority),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM review_job WHERE pr_id=? AND head_sha=?",
            (pr_id, head_sha),
        ).fetchone()["id"]
    if row["status"] in ("done", "failed", "canceled"):
        conn.execute(
            """UPDATE review_job SET status='queued', trigger='manual',
               locked_by=NULL, locked_at=NULL, next_run_at=NULL,
               attempts=0, error=NULL WHERE id=?""",
            (row["id"],),
        )
        conn.commit()
    return row["id"]


STALE_LOCK_MINUTES = 30


def claim_next(conn, *, worker_id):
    """queued(또는 backoff 만료)인 잡 1건을 원자적으로 running 전이.

    ★개정: 다른 worker가 BEGIN IMMEDIATE를 선점하면 database is locked가
    날 수 있음 → busy_timeout(5s) 대기 후에도 실패하면 None(다음 tick 재시도).
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return None  # 다른 worker가 선점 — 이번 tick은 빈손
    try:
        row = conn.execute(
            """SELECT * FROM review_job
               WHERE status='queued'
                 AND (next_run_at IS NULL OR next_run_at <= datetime('now'))
               ORDER BY priority DESC, id ASC LIMIT 1""",
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            """UPDATE review_job SET status='running', locked_by=?,
               locked_at=datetime('now'), attempts=attempts+1 WHERE id=?""",
            (worker_id, row["id"]),
        )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
        return None
    return conn.execute("SELECT * FROM review_job WHERE id=?", (row["id"],)).fetchone()


def recover_stale(conn, *, older_than_minutes: int = STALE_LOCK_MINUTES) -> int:
    """★개정: worker 크래시로 running/locked 고착된 잡을 queued로 복구.
    worker 시작 시 1회 호출(단일 워커라 부팅 시점의 running은 전부 orphan
    → older_than_minutes=0). 반환 = 복구 건수."""
    try:
        cur = conn.execute(
            """UPDATE review_job SET status='queued', locked_by=NULL,
               error='recovered from stale lock'
               WHERE status='running'
                 AND locked_at <= datetime('now', ?)""",
            (f"-{older_than_minutes} minutes",),
        )
        conn.commit()
        return cur.rowcount
    except sqlite3.OperationalError:
        conn.rollback()
        raise


def mark_done(conn, job_id, run_id):
    try:
        conn.execute(
            "UPDATE review_job SET status='done', run_id=?, locked_by=NULL WHERE id=?",
            (run_id, job_id),
        )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
        raise


def mark_failed(conn, job_id, *, error, retry: bool, run_id=None):
    # ★개정 (codex v3 [HIGH]): run_id는 이번 attempt의 (failed) review_run.
    # retry로 queued 되돌릴 때도 job.run_id에 최신 attempt run을 남겨,
    # failed run과 retry job이 갈라지지 않게 한다.
    # ★정책 (codex v4 [MEDIUM]): run_id=None은 **pre-run 실패**(build_deps/claim
    # 직후 등, review_run 생성 전). 이땐 COALESCE로 직전 run 포인터를 유지하되,
    # job.error는 최신(=pre-run) 에러다 → error↔run 짝은 pre-run 실패에선 보장 안 됨.
    # attempt별 정확한 상태는 review_run 테이블(pr_id로 조회)이 단일 진실원.
    try:
        row = conn.execute(
            "SELECT attempts, max_attempts FROM review_job WHERE id=?", (job_id,)
        ).fetchone()
        if retry and row["attempts"] < row["max_attempts"]:
            # 지수 backoff: 2^attempts 분 뒤 재시도
            conn.execute(
                """UPDATE review_job SET status='queued', locked_by=NULL,
                   error=?, run_id=COALESCE(?, run_id),
                   next_run_at=datetime('now', '+' ||
                   (1 << attempts) || ' minutes') WHERE id=?""",
                (error, run_id, job_id),
            )
        else:
            conn.execute(
                "UPDATE review_job SET status='failed', error=?, "
                "run_id=COALESCE(?, run_id), locked_by=NULL "
                "WHERE id=?",
                (error, run_id, job_id),
            )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
        raise
