import asyncio

from server.notify import notify_review_done
from server.pipeline import PipelineError, retry_pr, review_pr
from server.repos import job_repo, pr_repo, repo_repo, review_repo, settings_repo
from server.review.gh_deps import build_deps  # Task 7.2에서 정의

_RETRYABLE = ("rate limit", "rate_limit", "429", "overloaded", "timeout", "timed out")


def _is_retryable(msg: str) -> bool:
    m = msg.lower()
    return any(s in m for s in _RETRYABLE)


async def run_one_job(conn, *, worker_id: str) -> bool:
    """queued 잡 1건을 claim해 실행. 처리했으면 True."""
    job = job_repo.claim_next(conn, worker_id=worker_id)
    if job is None:
        return False
    try:
        pr = pr_repo.get(conn, job["pr_id"])
        # enqueue 후 PR가 닫히거나 병합됐을 수 있다(poller 재조정) — 벤더를 돌리기
        # 전에 걸러 토큰 낭비를 막는다. 큐 자체는 취소하지 않으므로 여기가 최종 게이트.
        if pr["state"] != "open":
            job_repo.mark_canceled(conn, job["id"], error="PR가 닫혀 리뷰 취소")
            return True
        repo = repo_repo.get(conn, pr["repo_id"])
        settings = settings_repo.get(conn)
        deps = build_deps(repo, settings)
        # trigger='retry'는 엔드포인트가 검증한 retry_run_id의 실패 벤더만 재실행(새 run 미생성).
        if job["trigger"] == "retry":
            run_id = await retry_pr(
                conn, pr_id=job["pr_id"], run_id=job["retry_run_id"], deps=deps
            )
        else:
            run_id = await review_pr(
                conn, pr_id=job["pr_id"], trigger=job["trigger"], deps=deps
            )
        job_repo.mark_done(conn, job["id"], run_id)
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM finding WHERE run_id=?", (run_id,)
        ).fetchone()["n"]
        notify_review_done(
            repo_full=repo["full_name"],
            pr_number=pr["number"],
            status="done",
            findings=n,
        )
    except PipelineError as e:
        # ★개정 (codex v3 [HIGH]): 실패한 attempt의 run_id를 job에 기록해
        # failed run과 retry job이 갈라지지 않게 한다.
        job_repo.mark_failed(
            conn, job["id"], error=str(e), retry=_is_retryable(str(e)), run_id=e.run_id
        )
        _notify_if_failed(conn, job)
    except Exception as e:
        # run 생성 이전(build_deps/claim 직후) 실패 → 연결된 run 없음.
        job_repo.mark_failed(
            conn, job["id"], error=str(e), retry=_is_retryable(str(e)), run_id=None
        )
        _notify_if_failed(conn, job)
    return True


def _notify_if_failed(conn, job):
    """터미널 failed일 때만 알림(재시도 대기 queued는 조용히). mark_failed가
    attempts/max_attempts로 최종 판정하므로 여기선 결과 상태를 다시 읽는다."""
    row = conn.execute(
        """SELECT j.status, pr.number, r.full_name FROM review_job j
           JOIN pull_request pr ON pr.id = j.pr_id
           JOIN repo r ON r.id = pr.repo_id WHERE j.id=?""",
        (job["id"],),
    ).fetchone()
    if row and row["status"] == "failed":
        notify_review_done(
            repo_full=row["full_name"],
            pr_number=row["number"],
            status="failed",
            findings=0,
        )


async def worker_loop(db_path, *, worker_id="w1", idle_sleep=2.0, stop_event=None):
    from server.db import connect

    # ★개정: 시작 시 이전 크래시로 고착된 running 잡을 queued로 복구.
    # 짝이 되는 run/vendor_result 'running' 유령 행도 failed로 마감(self-heal).
    boot = connect(db_path)
    try:
        n = job_repo.recover_stale(boot, older_than_minutes=0)
        if n:
            print(f"[worker] recovered {n} stale jobs")
        r = review_repo.recover_stale_running(boot)
        if r:
            print(f"[worker] closed {r} stale running runs")
    finally:
        boot.close()

    while stop_event is None or not stop_event.is_set():
        conn = connect(db_path)
        try:
            worked = await run_one_job(conn, worker_id=worker_id)
        except Exception as e:  # 한 틱의 실패가 워커를 영구히 죽이지 않게
            print(f"[worker] tick failed: {e!r}")
            worked = False
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
