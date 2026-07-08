import asyncio

from server.worker import run_one_job
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
    monkeypatch.setattr("server.worker.build_deps", lambda repo: None)
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
