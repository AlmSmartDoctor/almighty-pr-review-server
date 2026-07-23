from server.repos import repo_repo, pr_repo, job_repo


def _seed(db, sha="s1"):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=1,
        title="t",
        author="a",
        head_sha=sha,
        base_ref="main",
        url="u",
    )
    return pid


def test_enqueue_is_idempotent_per_sha(db):
    pid = _seed(db)
    j1 = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    j2 = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    assert j1 == j2  # UNIQUE(pr_id, head_sha) → 같은 잡


def test_cancel_stale_job_atomically_enqueues_current_head(db):
    pid = _seed(db, sha="old")
    old_id = job_repo.enqueue(db, pr_id=pid, head_sha="old", trigger="manual")
    assert job_repo.claim_next(db, worker_id="w1")["id"] == old_id
    db.execute("UPDATE pull_request SET head_sha='new' WHERE id=?", (pid,))
    db.commit()

    new_id = job_repo.cancel_stale_and_enqueue_latest(db, old_id)

    old = db.execute("SELECT * FROM review_job WHERE id=?", (old_id,)).fetchone()
    new = db.execute("SELECT * FROM review_job WHERE id=?", (new_id,)).fetchone()
    assert old["status"] == "canceled" and old["error"] == job_repo.STALE_HEAD_CANCEL_ERROR
    assert new["head_sha"] == "new" and new["status"] == "queued"
    assert new["trigger"] == "manual" and new["retry_run_id"] is None


def test_cancel_stale_retry_requeues_latest_as_manual(db):
    from server.repos import review_repo

    pid = _seed(db, sha="old")
    run_id = review_repo.create_run(
        db, pr_id=pid, head_sha="old", trigger="manual", effort="medium"
    )
    review_repo.finish_run(db, run_id, "done")
    old_id = job_repo.enqueue_retry(
        db, pr_id=pid, head_sha="old", run_id=run_id
    )
    assert job_repo.claim_next(db, worker_id="w1")["id"] == old_id
    db.execute("UPDATE pull_request SET head_sha='new' WHERE id=?", (pid,))
    db.commit()

    new_id = job_repo.cancel_stale_and_enqueue_latest(db, old_id)
    new = db.execute("SELECT * FROM review_job WHERE id=?", (new_id,)).fetchone()
    assert new["trigger"] == "manual" and new["retry_run_id"] is None


def test_system_cancel_revive_does_not_overwrite_concurrent_worker_claim(tmp_path):
    from server.db import connect, init_schema

    path = tmp_path / "revive-race.db"
    primary = connect(path)
    init_schema(primary)
    pid = _seed(primary)
    jid = job_repo.enqueue(primary, pr_id=pid, head_sha="s1", trigger="auto")
    job_repo.mark_canceled(
        primary, jid, error=job_repo.DISABLED_REPO_CANCEL_ERROR
    )
    other = connect(path)

    class RacingConnection:
        def __init__(self, conn):
            self._conn = conn
            self.triggered = False

        def execute(self, sql, params=()):
            if not self.triggered and "UPDATE review_job SET status='queued'" in sql:
                self.triggered = True
                other.execute(
                    "UPDATE review_job SET status='queued' WHERE id=?", (jid,)
                )
                other.commit()
                claimed = job_repo.claim_next(other, worker_id="winner")
                assert claimed is not None
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    result = job_repo.enqueue_with_result(
        RacingConnection(primary), pr_id=pid, head_sha="s1", trigger="auto"
    )
    assert result == (jid, False)  # 다른 요청이 revive/claim했으므로 이 호출은 미집계
    row = primary.execute("SELECT * FROM review_job WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "running"
    assert row["locked_by"] == "winner"
    assert row["attempts"] == 1
    other.close()
    primary.close()


def test_manual_revive_conflicts_without_overwriting_concurrent_worker_claim(tmp_path):
    import pytest

    from server.db import connect, init_schema

    path = tmp_path / "manual-race.db"
    primary = connect(path)
    init_schema(primary)
    pid = _seed(primary)
    jid = job_repo.enqueue(primary, pr_id=pid, head_sha="s1", trigger="auto")
    job_repo.claim_next(primary, worker_id="initial")
    job_repo.mark_done(primary, jid, run_id=None)
    other = connect(path)

    class RacingConnection:
        def __init__(self, conn):
            self._conn = conn
            self.triggered = False

        def execute(self, sql, params=()):
            if not self.triggered and "UPDATE review_job SET status='queued'" in sql:
                self.triggered = True
                other.execute(
                    "UPDATE review_job SET status='queued' WHERE id=?", (jid,)
                )
                other.commit()
                assert job_repo.claim_next(other, worker_id="winner") is not None
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    with pytest.raises(ValueError, match="다른 리뷰 작업"):
        job_repo.enqueue_manual(
            RacingConnection(primary), pr_id=pid, head_sha="s1"
        )
    row = primary.execute("SELECT * FROM review_job WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "running"
    assert row["locked_by"] == "winner"
    assert row["attempts"] == 2
    other.close()
    primary.close()


def test_enqueue_manual_upgrades_queued_auto_but_rejects_running_auto(db):
    import pytest

    pid = _seed(db)
    running_id = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    job_repo.claim_next(db, worker_id="winner")
    with pytest.raises(ValueError, match="다른 리뷰 작업"):
        job_repo.enqueue_manual(db, pr_id=pid, head_sha="s1")
    running = db.execute(
        "SELECT status, trigger FROM review_job WHERE id=?", (running_id,)
    ).fetchone()
    assert running["status"] == "running"
    assert running["trigger"] == "auto"

    queued_id = job_repo.enqueue(db, pr_id=pid, head_sha="s2", trigger="auto")
    assert job_repo.enqueue_manual(db, pr_id=pid, head_sha="s2") == queued_id
    queued = db.execute(
        "SELECT status, trigger FROM review_job WHERE id=?", (queued_id,)
    ).fetchone()
    assert queued["status"] == "queued"
    assert queued["trigger"] == "manual"


def test_enqueue_manual_inserts_when_absent(db):
    pid = _seed(db)
    jid = job_repo.enqueue_manual(db, pr_id=pid, head_sha="s1")
    row = db.execute("SELECT * FROM review_job WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "queued" and row["trigger"] == "manual"


def test_concurrent_first_retry_insert_returns_conflict_instead_of_integrity_error(tmp_path):
    import pytest

    from server.db import connect, init_schema
    from server.repos import review_repo

    path = tmp_path / "retry-insert-race.db"
    primary = connect(path)
    init_schema(primary)
    pid = _seed(primary)
    run1 = review_repo.create_run(
        primary, pr_id=pid, head_sha="s1", trigger="manual", effort="medium"
    )
    run2 = review_repo.create_run(
        primary, pr_id=pid, head_sha="s1", trigger="manual", effort="medium"
    )
    other = connect(path)

    class RacingConnection:
        def __init__(self, conn):
            self._conn = conn
            self.triggered = False

        def execute(self, sql, params=()):
            if not self.triggered and "INSERT INTO review_job" in sql:
                self.triggered = True
                other.execute(
                    """INSERT INTO review_job
                       (pr_id, head_sha, trigger, retry_run_id, created_at)
                       VALUES (?, ?, 'retry', ?, datetime('now'))""",
                    (pid, "s1", run2),
                )
                other.commit()
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    with pytest.raises(ValueError, match="다른 리뷰 작업"):
        job_repo.enqueue_retry(
            RacingConnection(primary), pr_id=pid, head_sha="s1", run_id=run1
        )
    row = primary.execute("SELECT retry_run_id FROM review_job").fetchone()
    assert row["retry_run_id"] == run2
    other.close()
    primary.close()


def test_enqueue_manual_reopens_terminal_job(db):
    pid = _seed(db)
    jid = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    job_repo.claim_next(db, worker_id="w1")  # attempts→1, running
    job_repo.mark_done(db, jid, run_id=None)  # 종료(done)
    again = job_repo.enqueue_manual(db, pr_id=pid, head_sha="s1")
    assert again == jid  # 같은 잡을 재개
    row = db.execute("SELECT * FROM review_job WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "queued"  # 재리뷰 실제 재실행되게 재개
    assert row["attempts"] == 0  # attempts 리셋


def test_enqueue_manual_rejects_running_auto_job(db):
    import pytest

    pid = _seed(db)
    jid = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    job_repo.claim_next(db, worker_id="w1")  # running
    with pytest.raises(ValueError, match="다른 리뷰 작업"):
        job_repo.enqueue_manual(db, pr_id=pid, head_sha="s1")
    row = db.execute("SELECT * FROM review_job WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "running"  # 진행 중 잡은 안 건드림
    assert row["trigger"] == "auto"


def test_claim_next_locks_one_job(db):
    pid = _seed(db)
    job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    claimed = job_repo.claim_next(db, worker_id="w1")
    assert claimed["status"] == "running"
    assert claimed["locked_by"] == "w1"
    # 이미 running이면 다음 claim은 없음
    assert job_repo.claim_next(db, worker_id="w2") is None


def test_finish_and_retry(db):
    pid = _seed(db)
    job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    j = job_repo.claim_next(db, worker_id="w1")
    job_repo.mark_failed(db, j["id"], error="rate limit", retry=True)
    row = db.execute("SELECT * FROM review_job WHERE id=?", (j["id"],)).fetchone()
    assert row["status"] == "queued" and row["attempts"] == 1
    assert row["next_run_at"] is not None  # backoff 설정됨


def test_claim_blocks_on_writer_lock_then_recovers(tmp_path):
    from server.db import connect, init_schema
    from server.repos import repo_repo, pr_repo, job_repo

    p = tmp_path / "c.db"
    c0 = connect(p)
    init_schema(c0)
    rid = repo_repo.add(c0, full_name="acme/api")
    pid = pr_repo.upsert(
        c0,
        repo_id=rid,
        number=1,
        title="t",
        author="a",
        head_sha="s1",
        base_ref="main",
        url="u",
    )
    job_repo.enqueue(c0, pr_id=pid, head_sha="s1", trigger="auto")
    c1, c2 = connect(p), connect(p)
    c2.execute("PRAGMA busy_timeout=200")  # 5초 대신 200ms만 대기
    c1.execute("BEGIN IMMEDIATE")  # writer 락 선점
    assert job_repo.claim_next(c2, worker_id="w2") is None  # 락 경합 → None
    c1.rollback()  # 락 해제
    got = job_repo.claim_next(c2, worker_id="w2")  # 이제 성공
    assert got is not None and got["locked_by"] == "w2"
    for c in (c0, c1, c2):
        c.close()


def test_concurrent_claim_exactly_once(tmp_path):
    import threading
    from server.db import connect, init_schema
    from server.repos import repo_repo, pr_repo, job_repo

    p = tmp_path / "c.db"
    c0 = connect(p)
    init_schema(c0)
    rid = repo_repo.add(c0, full_name="acme/api")
    pid = pr_repo.upsert(
        c0,
        repo_id=rid,
        number=1,
        title="t",
        author="a",
        head_sha="s1",
        base_ref="main",
        url="u",
    )
    job_repo.enqueue(c0, pr_id=pid, head_sha="s1", trigger="auto")
    N = 8
    barrier = threading.Barrier(N)
    results = [None] * N

    def worker(i):
        conn = connect(p)
        try:
            barrier.wait()  # N개 스레드 동시 출발
            results[i] = job_repo.claim_next(conn, worker_id=f"w{i}")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1  # 정확히 하나만 claim
    c0.close()


def test_recover_stale_requeues_running(db):
    from server.repos import repo_repo, pr_repo, job_repo

    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=1,
        title="t",
        author="a",
        head_sha="s1",
        base_ref="main",
        url="u",
    )
    jid = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    # 오래된 running lock 위조
    db.execute(
        """UPDATE review_job SET status='running', locked_by='dead',
                  locked_at=datetime('now','-60 minutes') WHERE id=?""",
        (jid,),
    )
    db.commit()
    assert job_repo.recover_stale(db) == 1
    assert (
        db.execute("SELECT status FROM review_job WHERE id=?", (jid,)).fetchone()[
            "status"
        ]
        == "queued"
    )


def test_recover_stale_zero_recovers_fresh_lock(db):
    # 단일 워커 부팅: 방금 잠긴(2분 전) running도 orphan → older_than_minutes=0로 복구.
    pid = _seed(db)
    jid = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    db.execute(
        """UPDATE review_job SET status='running', locked_by='dead',
                  locked_at=datetime('now','-2 minutes') WHERE id=?""",
        (jid,),
    )
    db.commit()
    assert job_repo.recover_stale(db, older_than_minutes=0) == 1
    row = db.execute("SELECT * FROM review_job WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "queued"
    assert row["locked_by"] is None


def test_mark_failed_no_dangling_txn_on_lock_contention(tmp_path):
    import sqlite3
    import pytest
    from server.db import connect, init_schema
    from server.repos import repo_repo, pr_repo, job_repo

    p = tmp_path / "c.db"
    c0 = connect(p)
    init_schema(c0)
    rid = repo_repo.add(c0, full_name="acme/api")
    pid = pr_repo.upsert(
        c0,
        repo_id=rid,
        number=1,
        title="t",
        author="a",
        head_sha="s1",
        base_ref="main",
        url="u",
    )
    job_repo.enqueue(c0, pr_id=pid, head_sha="s1", trigger="auto")
    worker = connect(p)
    worker.execute("PRAGMA busy_timeout=200")  # don't wait 5s
    j = job_repo.claim_next(worker, worker_id="w1")
    assert j is not None
    blocker = connect(p)
    blocker.execute("BEGIN IMMEDIATE")  # steal the writer lock
    with pytest.raises(sqlite3.OperationalError):
        job_repo.mark_failed(worker, j["id"], error="x", retry=False)
    assert worker.in_transaction is False  # no dangling txn (the fix)
    blocker.rollback()  # release lock
    # connection still usable — claim_next must not be permanently broken:
    pid2 = pr_repo.upsert(
        c0,
        repo_id=rid,
        number=2,
        title="t",
        author="a",
        head_sha="s2",
        base_ref="main",
        url="u",
    )
    job_repo.enqueue(c0, pr_id=pid2, head_sha="s2", trigger="auto")
    assert job_repo.claim_next(worker, worker_id="w1") is not None
    for c in (c0, worker, blocker):
        c.close()


def test_enqueue_revives_closed_pr_cancel_on_reopen(db):
    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=9,
        title="t",
        author="a",
        head_sha="s1",
        base_ref="main",
        url="u",
    )
    jid = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    job_repo.mark_canceled(db, jid, error=job_repo.CLOSED_PR_CANCEL_ERROR)

    # PR reopen(같은 sha) → poller의 auto enqueue가 시스템 취소를 되살려야 한다
    assert job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto") == jid
    j = db.execute("SELECT * FROM review_job WHERE id=?", (jid,)).fetchone()
    assert j["status"] == "queued" and j["error"] is None and j["attempts"] == 0


def test_enqueue_does_not_revive_user_cancel(db):
    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=9,
        title="t",
        author="a",
        head_sha="s1",
        base_ref="main",
        url="u",
    )
    jid = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    job_repo.mark_canceled(db, jid, error="사용자가 취소")

    job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")  # 폴러 재시도
    j = db.execute("SELECT * FROM review_job WHERE id=?", (jid,)).fetchone()
    assert j["status"] == "canceled"  # 사용자 취소는 auto가 무력화하지 못한다


def test_cancel_queued_cancels_all_queued_but_not_running(db):
    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=9,
        title="t",
        author="a",
        head_sha="s2",
        base_ref="main",
        url="u",
    )
    j1 = job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    j2 = job_repo.enqueue(db, pr_id=pid, head_sha="s2", trigger="auto")
    j3 = job_repo.enqueue(db, pr_id=pid, head_sha="s3", trigger="auto")
    db.execute("UPDATE review_job SET status='running' WHERE id=?", (j3,))
    db.commit()

    assert job_repo.cancel_queued(db, pid, error="사용자가 취소") == 2
    statuses = {
        r["id"]: r["status"]
        for r in db.execute("SELECT id, status FROM review_job").fetchall()
    }
    assert statuses[j1] == "canceled" and statuses[j2] == "canceled"
    assert statuses[j3] == "running"  # 원자 status 가드 — running은 못 건드림
