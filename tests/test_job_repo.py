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
