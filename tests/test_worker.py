import asyncio

from server.worker import _is_retryable, run_one_job, worker_loop
from server.repos import repo_repo, pr_repo, job_repo, review_repo


def test_worker_claims_and_runs_job(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
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
    job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")

    async def fake_review_pr(conn, *, pr_id, trigger, deps):
        return review_repo.create_run(
            conn, pr_id=pr_id, head_sha="s1", trigger=trigger, effort="medium"
        )

    monkeypatch.setattr("server.worker.review_pr", fake_review_pr)
    claimed = asyncio.run(run_one_job(db, worker_id="w1"))
    assert claimed is True
    j = db.execute("SELECT * FROM review_job WHERE pr_id=?", (pid,)).fetchone()
    assert j["status"] == "done" and j["run_id"] is not None


def test_worker_routes_retry_trigger_to_retry_pr(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
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
    target = review_repo.create_run(
        db, pr_id=pid, head_sha="s1", trigger="manual", effort="medium"
    )
    job_repo.enqueue_retry(db, pr_id=pid, head_sha="s1", run_id=target)

    calls = {}

    async def fake_retry_pr(conn, *, pr_id, run_id, deps):
        calls["retry"] = (pr_id, run_id)
        return run_id

    async def fake_review_pr(conn, *, pr_id, trigger, deps):
        calls["review"] = pr_id
        return 0

    monkeypatch.setattr("server.worker.build_deps", lambda repo, settings: None)
    monkeypatch.setattr("server.worker.retry_pr", fake_retry_pr)
    monkeypatch.setattr("server.worker.review_pr", fake_review_pr)
    asyncio.run(run_one_job(db, worker_id="w1"))
    # retry 트리거는 retry_pr만 호출(review_pr 아님) + 검증된 run_id 전파
    assert calls == {"retry": (pid, target)}
    j = db.execute("SELECT status FROM review_job WHERE pr_id=?", (pid,)).fetchone()
    assert j["status"] == "done"


def test_worker_marks_failed_with_retry(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=2,
        title="t",
        author="a",
        head_sha="s2",
        base_ref="main",
        url="u",
    )
    job_repo.enqueue(db, pr_id=pid, head_sha="s2", trigger="auto")

    async def boom(conn, *, pr_id, trigger, deps):
        raise RuntimeError("rate limit")

    monkeypatch.setattr("server.worker.review_pr", boom)
    asyncio.run(run_one_job(db, worker_id="w1"))
    j = db.execute("SELECT * FROM review_job WHERE pr_id=?", (pid,)).fetchone()
    assert j["status"] == "queued" and j["attempts"] == 1  # 재시도 예약


def test_worker_records_failed_run_id_on_pipeline_error(db, monkeypatch):
    """★개정 (codex v5 [LOW]): review_pr가 PipelineError(run_id)를 던지면
    worker가 그 실패 attempt run을 retry job에 기록하는 통합 경로 검증."""
    from server.pipeline import PipelineError
    from server.repos import review_repo

    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=3,
        title="t",
        author="a",
        head_sha="s3",
        base_ref="main",
        url="u",
    )
    job_repo.enqueue(db, pr_id=pid, head_sha="s3", trigger="auto")

    async def boom(conn, *, pr_id, trigger, deps):
        run_id = review_repo.create_run(
            conn, pr_id=pr_id, head_sha="s3", trigger=trigger, effort="medium"
        )
        review_repo.finish_run(conn, run_id, "failed", error="all vendors failed")
        raise PipelineError(run_id, "all vendors failed → rate limit")

    # ★개정 (codex v6 [LOW]): build_deps는 real 호출을 피해 monkeypatch(환경 비의존).
    monkeypatch.setattr("server.worker.build_deps", lambda repo, settings: None)
    monkeypatch.setattr("server.worker.review_pr", boom)
    asyncio.run(run_one_job(db, worker_id="w1"))
    j = db.execute("SELECT * FROM review_job WHERE pr_id=?", (pid,)).fetchone()
    assert j["status"] == "queued"  # rate → retry 예약
    assert j["run_id"] is not None  # 실패 attempt run 기록
    assert review_repo.get_run(db, j["run_id"])["status"] == "failed"


def test_worker_returns_false_when_queue_empty(db):
    # 큐가 비면 claim_next→None → run_one_job이 아무 것도 처리하지 않고 False
    claimed = asyncio.run(run_one_job(db, worker_id="w1"))
    assert claimed is False


def test_worker_marks_failed_no_retry_on_non_retryable(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=4,
        title="t",
        author="a",
        head_sha="s4",
        base_ref="main",
        url="u",
    )
    job_repo.enqueue(db, pr_id=pid, head_sha="s4", trigger="auto")

    async def boom(conn, *, pr_id, trigger, deps):
        raise RuntimeError("permission denied")  # rate/timeout 아님 → 재시도 안 함

    monkeypatch.setattr("server.worker.review_pr", boom)
    asyncio.run(run_one_job(db, worker_id="w1"))
    j = db.execute("SELECT * FROM review_job WHERE pr_id=?", (pid,)).fetchone()
    assert j["status"] == "failed"  # 비재시도 오류 → 즉시 failed (retry 분기 else)


def test_is_retryable_classification():
    assert _is_retryable("failed to generate output") is False  # "generate" 오탐 방지
    assert _is_retryable("rate limit exceeded") is True
    assert _is_retryable("HTTP 429") is True
    assert _is_retryable("vendor timeout after 600s") is True
    assert _is_retryable("overloaded") is True


def test_run_one_job_marks_failed_when_pr_missing(db, monkeypatch):
    # pr_repo.get가 None을 주면(레이스로 삭제 등) 예외로 죽지 않고 job을 failed로.
    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=5,
        title="t",
        author="a",
        head_sha="s5",
        base_ref="main",
        url="u",
    )
    job_repo.enqueue(db, pr_id=pid, head_sha="s5", trigger="auto")
    monkeypatch.setattr("server.worker.pr_repo.get", lambda conn, pid: None)
    claimed = asyncio.run(run_one_job(db, worker_id="w1"))
    assert claimed is True  # 처리는 함(예외 미전파)
    j = db.execute("SELECT * FROM review_job WHERE pr_id=?", (pid,)).fetchone()
    assert j["status"] == "failed"  # stranded running 방지


def test_worker_cancels_job_for_closed_pr(db, monkeypatch):
    # enqueue 후 PR가 닫히면(poller 재조정) 벤더를 돌리지 않고 잡을 canceled로 마감.
    rid = repo_repo.add(db, full_name="acme/api", local_path="/tmp/acme")
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
    job_repo.enqueue(db, pr_id=pid, head_sha="s1", trigger="auto")
    pr_repo.mark_closed(db, rid, {1})

    async def must_not_run(conn, **kw):
        raise AssertionError("closed PR must not be reviewed")

    monkeypatch.setattr("server.worker.review_pr", must_not_run)
    assert asyncio.run(run_one_job(db, worker_id="w1")) is True
    j = db.execute(
        "SELECT status, error FROM review_job WHERE pr_id=?", (pid,)
    ).fetchone()
    assert j["status"] == "canceled" and "닫혀" in j["error"]


def test_worker_boot_closes_stale_running_runs(tmp_path, monkeypatch):
    # 크래시로 'running'에 고착된 run/vendor_result는 부팅 시 failed로 마감된다
    # (잡만 복구하면 유령 running 행이 영원히 duration 틱업하며 남는다).
    from server.db import connect, init_schema

    c = connect(tmp_path / "w.db")
    init_schema(c)
    rid = repo_repo.add(c, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(
        c,
        repo_id=rid,
        number=1,
        title="t",
        author="a",
        head_sha="s1",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        c, pr_id=pid, head_sha="s1", trigger="auto", effort="medium"
    )  # status='running'
    review_repo.add_vendor_result(c, run_id=run_id, vendor="claude", status="running")
    c.close()

    stop = asyncio.Event()

    async def fake_run_one_job(conn, *, worker_id):
        stop.set()
        return False

    monkeypatch.setattr("server.worker.run_one_job", fake_run_one_job)
    asyncio.run(worker_loop(tmp_path / "w.db", stop_event=stop, idle_sleep=0.01))

    c = connect(tmp_path / "w.db")
    run = review_repo.get_run(c, run_id)
    assert run["status"] == "failed" and "재시작" in run["error"]
    assert run["finished_at"] is not None
    vr = c.execute("SELECT status, error FROM vendor_result").fetchone()
    assert vr["status"] == "failed" and "재시작" in vr["error"]
    c.close()


def test_worker_loop_survives_tick_error(tmp_path, monkeypatch):
    from server.db import connect, init_schema

    c = connect(tmp_path / "w.db")
    init_schema(c)
    c.close()

    stop = asyncio.Event()
    calls = []

    async def fake_run_one_job(conn, *, worker_id):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("tick boom")  # 첫 틱은 폭발
        stop.set()
        return False

    monkeypatch.setattr("server.worker.run_one_job", fake_run_one_job)
    asyncio.run(worker_loop(tmp_path / "w.db", stop_event=stop, idle_sleep=0.01))
    assert len(calls) >= 2  # 첫 에러 후에도 루프 생존
