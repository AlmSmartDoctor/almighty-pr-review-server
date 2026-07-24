import sqlite3

# 시스템 취소 식별자 — 사용자 취소와 달리 PR/레포 설정이 다시 유효해지면 auto enqueue가
# 되살려야 한다(아니면 같은 sha가 영원히 리뷰되지 않는 교착).
CLOSED_PR_CANCEL_ERROR = "PR가 닫혀 리뷰 취소"
DISABLED_REPO_CANCEL_ERROR = "레포가 비활성화되어 리뷰 취소"
NO_VENDOR_CANCEL_ERROR = "활성 리뷰 vendor가 없어 리뷰 취소"
STALE_HEAD_CANCEL_ERROR = "PR head가 변경되어 이전 리뷰 잡 취소"
_SYSTEM_CANCEL_ERRORS = {
    CLOSED_PR_CANCEL_ERROR,
    DISABLED_REPO_CANCEL_ERROR,
    NO_VENDOR_CANCEL_ERROR,
    STALE_HEAD_CANCEL_ERROR,
}


def is_system_cancel_error(error: str | None) -> bool:
    return error in _SYSTEM_CANCEL_ERRORS


def enqueue_with_result(
    conn, *, pr_id, head_sha, trigger, priority=0, commit=True
) -> tuple[int, bool]:
    """자동 enqueue 결과를 원자적 rowcount 기준으로 반환한다.

    bool은 이 호출이 새 행을 만들거나 system-canceled 행을 실제로 queued로 복구했을 때만
    True다. 사전 SELECT 결과만으로 추측하지 않아 worker claim과의 경합에서도 정확하다.
    """
    cur = conn.execute(
        """INSERT INTO review_job (pr_id, head_sha, trigger, priority, created_at)
           VALUES (?,?,?,?, datetime('now'))
           ON CONFLICT(pr_id, head_sha) DO NOTHING""",
        (pr_id, head_sha, trigger, priority),
    )
    inserted = cur.rowcount == 1
    if commit:
        conn.commit()
    row = conn.execute(
        "SELECT id, status, error FROM review_job WHERE pr_id=? AND head_sha=?",
        (pr_id, head_sha),
    ).fetchone()
    revived = False
    if row["status"] == "canceled" and is_system_cancel_error(row["error"]):
        cur = conn.execute(
            """UPDATE review_job SET status='queued', trigger=?, locked_by=NULL,
               locked_at=NULL, next_run_at=NULL, attempts=0, error=NULL
               WHERE id=? AND status='canceled' AND error=?""",
            (trigger, row["id"], row["error"]),
        )
        if commit:
            conn.commit()
        revived = cur.rowcount == 1
    return row["id"], inserted or revived


def enqueue(conn, *, pr_id, head_sha, trigger, priority=0, commit=True) -> int:
    return enqueue_with_result(
        conn,
        pr_id=pr_id,
        head_sha=head_sha,
        trigger=trigger,
        priority=priority,
        commit=commit,
    )[0]


def _enqueue_or_revive(
    conn, *, pr_id, head_sha, trigger, priority=0, retry_run_id=None
) -> int:
    """같은 (pr, sha)에 종료된(done/failed/canceled) 잡이 있으면 queued로 재개해
    재리뷰가 실제로 재실행되게 한다. 진행 중(queued/running)이면 건드리지 않고 id만
    반환. (auto 폴링의 enqueue는 DO NOTHING 멱등성 유지.) retry_run_id는
    worker가 trigger='retry'일 때만 읽는다."""
    row = conn.execute(
        """SELECT id, status, trigger, retry_run_id FROM review_job
           WHERE pr_id=? AND head_sha=?""",
        (pr_id, head_sha),
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO review_job
               (pr_id, head_sha, trigger, priority, retry_run_id, created_at)
               VALUES (?,?,?,?,?, datetime('now'))
               ON CONFLICT(pr_id, head_sha) DO NOTHING""",
            (pr_id, head_sha, trigger, priority, retry_run_id),
        )
        conn.commit()
        # 동시 요청이 INSERT를 선점했을 수 있으므로 실제 승자 행을 다시 읽고 아래에서
        # trigger/retry_run_id 계약을 검증한다.
        row = conn.execute(
            """SELECT id, status, trigger, retry_run_id FROM review_job
               WHERE pr_id=? AND head_sha=?""",
            (pr_id, head_sha),
        ).fetchone()
    if row["status"] in ("done", "failed", "canceled"):
        conn.execute(
            """UPDATE review_job SET status='queued', trigger=?, retry_run_id=?,
               locked_by=NULL, locked_at=NULL, next_run_at=NULL,
               attempts=0, error=NULL
               WHERE id=? AND status IN ('done', 'failed', 'canceled')""",
            (trigger, retry_run_id, row["id"]),
        )
        conn.commit()
    current = conn.execute(
        "SELECT status, trigger, retry_run_id FROM review_job WHERE id=?",
        (row["id"],),
    ).fetchone()
    if (
        trigger == "manual"
        and current["status"] == "queued"
        and current["trigger"] == "auto"
    ):
        conn.execute(
            """UPDATE review_job SET trigger='manual'
               WHERE id=? AND status='queued' AND trigger='auto'""",
            (row["id"],),
        )
        conn.commit()
        current = conn.execute(
            "SELECT status, trigger, retry_run_id FROM review_job WHERE id=?",
            (row["id"],),
        ).fetchone()
    if current["status"] in ("queued", "running") and (
        (
            trigger == "retry"
            and (
                current["trigger"] != "retry"
                or current["retry_run_id"] != retry_run_id
            )
        )
        or (trigger == "manual" and current["trigger"] != "manual")
    ):
        raise ValueError("다른 리뷰 작업이 이미 같은 PR head에서 진행 중입니다")
    return row["id"]


def enqueue_manual(conn, *, pr_id, head_sha, priority=0) -> int:
    return _enqueue_or_revive(
        conn, pr_id=pr_id, head_sha=head_sha, trigger="manual", priority=priority
    )


def retry_enqueue_conflict(conn, *, pr_id, head_sha, run_id) -> bool:
    """Read-only parity check for enqueue_retry's active same-head conflict."""
    current = conn.execute(
        """SELECT status, trigger, retry_run_id FROM review_job
           WHERE pr_id=? AND head_sha=?""",
        (pr_id, head_sha),
    ).fetchone()
    return bool(
        current
        and current["status"] in {"queued", "running"}
        and (
            current["trigger"] != "retry"
            or current["retry_run_id"] != run_id
        )
    )


def enqueue_retry(conn, *, pr_id, head_sha, run_id) -> int:
    """부분 재시도: trigger='retry' + retry_run_id로 이어받을 대상 run을 명시해,
    worker가 latest run이 아니라 **엔드포인트가 검증한 바로 그 run**의 실패 벤더만
    재실행하게 한다."""
    return _enqueue_or_revive(
        conn, pr_id=pr_id, head_sha=head_sha, trigger="retry", retry_run_id=run_id
    )


STALE_LOCK_MINUTES = 30


class LeaseLostError(RuntimeError):
    pass


def claim_next(conn, *, worker_id, owner_process_id=None):
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
               locked_at=datetime('now'), owner_process_id=?, attempts=attempts+1
               WHERE id=?""",
            (worker_id, owner_process_id, row["id"]),
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


def mark_done(conn, job_id, run_id, *, owner_process_id=None):
    try:
        cur = conn.execute(
            """UPDATE review_job SET status='done', run_id=?, locked_by=NULL,
                      locked_at=NULL, owner_process_id=NULL
               WHERE id=? AND (? IS NULL OR owner_process_id=?)""",
            (run_id, job_id, owner_process_id, owner_process_id),
        )
        if cur.rowcount != 1:
            raise LeaseLostError(f"job {job_id} lease lost")
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
        raise


def mark_canceled(conn, job_id, *, error, owner_process_id=None):
    """실행 전 무의미해진 잡(닫힌 PR 등)을 canceled로 마감 — 벤더 비용을 쓰지 않는다."""
    try:
        cur = conn.execute(
            """UPDATE review_job SET status='canceled', error=?, locked_by=NULL,
                      locked_at=NULL, owner_process_id=NULL
               WHERE id=? AND (? IS NULL OR owner_process_id=?)""",
            (error, job_id, owner_process_id, owner_process_id),
        )
        if cur.rowcount != 1:
            raise LeaseLostError(f"job {job_id} lease lost")
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
        raise


def cancel_stale_and_enqueue_latest(
    conn, job_id: int, *, owner_process_id=None
) -> int | None:
    """stale running/queued job 취소와 현재 head 대체 job 생성을 한 transaction으로 처리.

    최신 SHA job이 이미 있으면 그대로 재사용한다. system-canceled만 revive하고 사용자가
    취소한 job이나 이미 끝난 job은 되살리지 않는다. retry는 옛 run에 결과를 섞지 않도록
    최신 head의 manual 전체/증분 리뷰로 전환한다.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT * FROM review_job WHERE id=?", (job_id,)
        ).fetchone()
        if job is None:
            conn.rollback()
            return None
        pr = conn.execute(
            "SELECT head_sha FROM pull_request WHERE id=?", (job["pr_id"],)
        ).fetchone()
        conn.execute(
            """UPDATE review_job SET status='canceled', error=?, locked_by=NULL,
                      locked_at=NULL, owner_process_id=NULL
               WHERE id=? AND status IN ('queued','running')
                 AND (? IS NULL OR owner_process_id=?)""",
            (STALE_HEAD_CANCEL_ERROR, job_id, owner_process_id, owner_process_id),
        )
        if pr is None or not pr["head_sha"] or pr["head_sha"] == job["head_sha"]:
            conn.commit()
            return None

        trigger = "auto" if job["trigger"] == "auto" else "manual"
        conn.execute(
            """INSERT INTO review_job
               (pr_id, head_sha, trigger, priority, created_at)
               VALUES (?,?,?,?, datetime('now'))
               ON CONFLICT(pr_id, head_sha) DO NOTHING""",
            (job["pr_id"], pr["head_sha"], trigger, job["priority"]),
        )
        latest = conn.execute(
            "SELECT id, status, error FROM review_job WHERE pr_id=? AND head_sha=?",
            (job["pr_id"], pr["head_sha"]),
        ).fetchone()
        if latest["status"] == "canceled" and is_system_cancel_error(latest["error"]):
            conn.execute(
                """UPDATE review_job SET status='queued', trigger=?, retry_run_id=NULL,
                          locked_by=NULL, locked_at=NULL, next_run_at=NULL,
                          attempts=0, error=NULL
                   WHERE id=? AND status='canceled' AND error=?""",
                (trigger, latest["id"], latest["error"]),
            )
        conn.commit()
        return latest["id"]
    except Exception:
        conn.rollback()
        raise


def cancel_queued(conn, pr_id, *, error) -> int:
    """PR의 queued 잡 전부를 원자적으로 취소. status 가드가 있어 worker가 claim한
    (running) 잡은 건드리지 않는다 — 취소 API↔claim 레이스에서 안전. 반환=취소 건수."""
    try:
        cur = conn.execute(
            "UPDATE review_job SET status='canceled', error=?, locked_by=NULL "
            "WHERE pr_id=? AND status='queued'",
            (error, pr_id),
        )
        conn.commit()
        return cur.rowcount
    except sqlite3.OperationalError:
        conn.rollback()
        raise


def mark_failed(
    conn, job_id, *, error, retry: bool, run_id=None, owner_process_id=None
):
    # ★개정 (codex v3 [HIGH]): run_id는 이번 attempt의 (failed) review_run.
    # retry로 queued 되돌릴 때도 job.run_id에 최신 attempt run을 남겨,
    # failed run과 retry job이 갈라지지 않게 한다.
    # ★정책 (codex v4 [MEDIUM]): run_id=None은 **pre-run 실패**(build_deps/claim
    # 직후 등, review_run 생성 전). 이땐 COALESCE로 직전 run 포인터를 유지하되,
    # job.error는 최신(=pre-run) 에러다 → error↔run 짝은 pre-run 실패에선 보장 안 됨.
    # attempt별 정확한 상태는 review_run 테이블(pr_id로 조회)이 단일 진실원.
    try:
        row = conn.execute(
            "SELECT attempts, max_attempts, owner_process_id FROM review_job WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None or (
            owner_process_id is not None
            and row["owner_process_id"] != owner_process_id
        ):
            raise LeaseLostError(f"job {job_id} lease lost")
        if retry and row["attempts"] < row["max_attempts"]:
            # 지수 backoff: 2^attempts 분 뒤 재시도
            conn.execute(
                """UPDATE review_job SET status='queued', locked_by=NULL,
                   error=?, run_id=COALESCE(?, run_id), owner_process_id=NULL,
                   locked_at=NULL, next_run_at=datetime('now', '+' ||
                   (1 << attempts) || ' minutes') WHERE id=?
                   AND (? IS NULL OR owner_process_id=?)""",
                (error, run_id, job_id, owner_process_id, owner_process_id),
            )
        else:
            conn.execute(
                "UPDATE review_job SET status='failed', error=?, "
                "run_id=COALESCE(?, run_id), locked_by=NULL, locked_at=NULL, "
                "owner_process_id=NULL WHERE id=? "
                "AND (? IS NULL OR owner_process_id=?)",
                (error, run_id, job_id, owner_process_id, owner_process_id),
            )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
        raise
