import asyncio

from server.pipeline import PipelineError, review_pr
from server.repos import job_repo, pr_repo, repo_repo
from server.review.gh_deps import build_deps  # Task 7.2에서 정의


async def run_one_job(conn, *, worker_id: str) -> bool:
    """queued 잡 1건을 claim해 실행. 처리했으면 True."""
    job = job_repo.claim_next(conn, worker_id=worker_id)
    if job is None:
        return False
    pr = pr_repo.get(conn, job["pr_id"])
    repo = repo_repo.get(conn, pr["repo_id"])
    try:
        deps = build_deps(repo)
        run_id = await review_pr(
            conn, pr_id=job["pr_id"], trigger=job["trigger"], deps=deps
        )
        job_repo.mark_done(conn, job["id"], run_id)
    except PipelineError as e:
        # ★개정 (codex v3 [HIGH]): 실패한 attempt의 run_id를 job에 기록해
        # failed run과 retry job이 갈라지지 않게 한다.
        retry = "rate" in str(e).lower() or "timeout" in str(e).lower()
        job_repo.mark_failed(
            conn, job["id"], error=str(e), retry=retry, run_id=e.run_id
        )
    except Exception as e:
        # run 생성 이전(build_deps/claim 직후) 실패 → 연결된 run 없음.
        retry = "rate" in str(e).lower() or "timeout" in str(e).lower()
        job_repo.mark_failed(conn, job["id"], error=str(e), retry=retry, run_id=None)
    return True


async def worker_loop(db_path, *, worker_id="w1", idle_sleep=2.0, stop_event=None):
    from server.db import connect

    # ★개정: 시작 시 이전 크래시로 고착된 running 잡을 queued로 복구.
    boot = connect(db_path)
    try:
        n = job_repo.recover_stale(boot)
        if n:
            print(f"[worker] recovered {n} stale jobs")
    finally:
        boot.close()

    while stop_event is None or not stop_event.is_set():
        conn = connect(db_path)
        try:
            worked = await run_one_job(conn, worker_id=worker_id)
        finally:
            conn.close()
        if not worked:
            # ★개정 (codex v3 [MEDIUM]): stop_event 없으면 plain sleep으로 idle.
            # (stop_event.wait()에 AttributeError를 내며 busy loop 도는 것 방지)
            if stop_event is None:
                await asyncio.sleep(idle_sleep)
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=idle_sleep)
                except asyncio.TimeoutError:
                    pass  # idle 대기 만료 → 다음 tick
