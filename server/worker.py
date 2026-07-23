import asyncio

from server import config
from server.context.base import redact_secrets
from server.notify import notify_review_done
from server.pipeline import (
    PipelineError,
    PipelineLeaseLost,
    PipelineStaleHead,
    retry_pr,
    review_pr,
)
from server.repos import (
    job_repo,
    pr_repo,
    repo_repo,
    review_repo,
    settings_repo,
    wiki_repo,
)
from server.review.gh_deps import build_deps  # Task 7.2에서 정의

_RETRYABLE = (
    "rate limit",
    "rate_limit",
    "429",
    "overloaded",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "connection reset",
    "connection refused",
    "502",
    "503",
    "504",
)


def _is_retryable(msg: str) -> bool:
    m = msg.lower()
    return any(s in m for s in _RETRYABLE)


async def run_one_job(
    conn, *, worker_id: str, pool=None, owner_process_id=None
) -> bool:
    """queued 잡 1건을 claim해 실행. 처리했으면 True."""
    job = job_repo.claim_next(
        conn, worker_id=worker_id, owner_process_id=owner_process_id
    )
    if job is None:
        return False
    try:
        pr = pr_repo.get(conn, job["pr_id"])
        # enqueue 후 PR가 닫히거나 병합됐을 수 있다(poller 재조정) — 벤더를 돌리기
        # 전에 걸러 토큰 낭비를 막는다. 큐 자체는 취소하지 않으므로 여기가 최종 게이트.
        if pr["state"] != "open":
            job_repo.mark_canceled(
                conn, job["id"], error=job_repo.CLOSED_PR_CANCEL_ERROR,
                owner_process_id=owner_process_id
            )
            return True
        if pr["head_sha"] != job["head_sha"]:
            job_repo.cancel_stale_and_enqueue_latest(
                conn, job["id"], owner_process_id=owner_process_id
            )
            return True
        repo = repo_repo.get(conn, pr["repo_id"])
        if repo is None:
            raise RuntimeError("repo not found")
        if not repo["enabled"]:
            job_repo.mark_canceled(
                conn, job["id"], error=job_repo.DISABLED_REPO_CANCEL_ERROR,
                owner_process_id=owner_process_id
            )
            return True
        if not (repo["vendor_claude_on"] or repo["vendor_codex_on"]):
            job_repo.mark_canceled(
                conn, job["id"], error=job_repo.NO_VENDOR_CANCEL_ERROR,
                owner_process_id=owner_process_id
            )
            return True
        settings = settings_repo.get(conn)
        deps = (
            build_deps(repo, settings, pool=pool)
            if pool is not None
            else build_deps(repo, settings)
        )
        # trigger='retry'는 엔드포인트가 검증한 retry_run_id의 실패 벤더만 재실행(새 run 미생성).
        owner_kwargs = (
            {"owner_process_id": owner_process_id} if owner_process_id else {}
        )
        if job["trigger"] == "retry":
            pipeline_task = retry_pr(
                conn,
                pr_id=job["pr_id"],
                run_id=job["retry_run_id"],
                deps=deps,
                expected_head_sha=job["head_sha"],
                **owner_kwargs,
            )
        else:
            pipeline_task = review_pr(
                conn,
                pr_id=job["pr_id"],
                trigger=job["trigger"],
                deps=deps,
                expected_head_sha=job["head_sha"],
                **({"owner_job_id": job["id"]} if owner_process_id else {}),
                **owner_kwargs,
            )
        run_id = await asyncio.wait_for(
            pipeline_task, timeout=config.JOB_TIMEOUT_SEC
        )
        job_repo.mark_done(
            conn, job["id"], run_id, owner_process_id=owner_process_id
        )
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM finding WHERE run_id=?", (run_id,)
        ).fetchone()["n"]
        notify_review_done(
            repo_full=repo["full_name"],
            pr_number=pr["number"],
            status="done",
            findings=n,
        )
    except asyncio.TimeoutError:
        error = f"job timed out after {config.JOB_TIMEOUT_SEC}s"
        conn.execute(
            """UPDATE vendor_result SET status='failed', error=?
               WHERE status='running' AND run_id IN (
                 SELECT id FROM review_run WHERE owner_job_id=? AND status='running')""",
            (error, job["id"]),
        )
        conn.execute(
            """UPDATE review_run SET status='failed', error=?, finished_at=datetime('now')
               WHERE owner_job_id=? AND status='running'""",
            (error, job["id"]),
        )
        conn.commit()
        job_repo.mark_failed(
            conn, job["id"], error=error, retry=True,
            owner_process_id=owner_process_id
        )
    except PipelineLeaseLost:
        # 다른 프로세스가 만료 lease를 회수했다. 이전 owner는 어떤 상태도 덮지 않는다.
        pass
    except PipelineStaleHead:
        # 실패/backoff 예산을 쓰지 않고 최신 head를 즉시 재큐한다. retry도 최신 head의
        # manual 전체/증분 리뷰로 전환해 옛 run에 결과를 섞지 않는다.
        job_repo.cancel_stale_and_enqueue_latest(
            conn, job["id"], owner_process_id=owner_process_id
        )
    except job_repo.LeaseLostError:
        pass
    except PipelineError as e:
        # ★개정 (codex v3 [HIGH]): 실패한 attempt의 run_id를 job에 기록해
        # failed run과 retry job이 갈라지지 않게 한다.
        job_repo.mark_failed(
            conn, job["id"], error=str(e), retry=_is_retryable(str(e)), run_id=e.run_id,
            owner_process_id=owner_process_id
        )
        _notify_if_failed(conn, job)
    except Exception as e:
        # run 생성 이전(build_deps/claim 직후) 실패 → 연결된 run 없음.
        job_repo.mark_failed(
            conn, job["id"], error=str(e), retry=_is_retryable(str(e)), run_id=None,
            owner_process_id=owner_process_id
        )
        _notify_if_failed(conn, job)
    return True


async def run_one_wiki_job(
    conn, *, worker_id: str, generator=None, owner_process_id=None
) -> bool:
    """대기 중인 Wiki 생성 요청 한 건을 claim해 완료 상태까지 기록한다."""
    repo_id = wiki_repo.claim_next(
        conn, worker_id=worker_id, owner_process_id=owner_process_id
    )
    if repo_id is None:
        return False
    repo = repo_repo.get(conn, repo_id)
    if repo is None:  # 레포 삭제와 claim 사이의 방어적 레이스 처리
        return True
    try:
        if generator is None:
            from server.wiki import GroundTruthGenerator

            generator = GroundTruthGenerator()
        settings = settings_repo.get(conn)
        page, sources, source_sha = await generator.generate(repo, settings)
        wiki_repo.save(
            conn, repo_id, page=page, sources=sources, source_sha=source_sha,
            owner_process_id=owner_process_id
        )
    except Exception as exc:
        message = redact_secrets(f"{type(exc).__name__}: {exc}")
        retrying = _is_retryable(message) and wiki_repo.schedule_retry(
            conn, repo_id, message, owner_process_id=owner_process_id
        )
        if not retrying:
            wiki_repo.mark_failed(
                conn, repo_id, message, owner_process_id=owner_process_id
            )
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


def recover_worker_state(db_path) -> None:
    """만료 lease 또는 30분 넘은 legacy 작업만 복구한다."""
    from server.db import connect
    from server.repos import process_repo

    boot = connect(db_path)
    try:
        recovered = process_repo.recover_expired(boot)
        if any(recovered.values()):
            print(f"[worker] recovered expired work: {recovered}")
    finally:
        boot.close()


async def lease_heartbeat_loop(
    db_path, process_id: str, *, stop_event, interval_seconds=15, ttl_seconds=60
):
    from server.db import connect
    from server.repos import process_repo

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            break
        except asyncio.TimeoutError:
            pass
        conn = connect(db_path)
        try:
            if not process_repo.heartbeat(
                conn, process_id, ttl_seconds=ttl_seconds
            ):
                print("[worker] process lease lost; stopping background loops")
                stop_event.set()
                return
            process_repo.recover_expired(conn)
        finally:
            conn.close()


async def worker_loop(
    db_path,
    *,
    worker_id="w1",
    owner_process_id=None,
    idle_sleep=2.0,
    stop_event=None,
    pool=None,
    recover=True,
    wiki_enabled=True,
):
    from server.db import connect

    if recover:
        recover_worker_state(db_path)

    idle_delay = idle_sleep
    while stop_event is None or not stop_event.is_set():
        conn = connect(db_path)
        try:
            run_kwargs = {}
            if pool is not None:
                run_kwargs["pool"] = pool
            if owner_process_id is not None:
                run_kwargs["owner_process_id"] = owner_process_id
            review_worked = await run_one_job(
                conn, worker_id=worker_id, **run_kwargs
            )
            wiki_worked = (
                await run_one_wiki_job(
                    conn,
                    worker_id=worker_id,
                    **({"owner_process_id": owner_process_id}
                       if owner_process_id is not None else {}),
                )
                if wiki_enabled
                else False
            )
            worked = review_worked or wiki_worked
        except Exception as e:  # 한 틱의 실패가 워커를 영구히 죽이지 않게
            print(f"[worker] tick failed: {e!r}")
            worked = False
        finally:
            conn.close()
        if worked:
            idle_delay = idle_sleep
            continue
        # idle DB claim 빈도를 2→4→8… 최대 30초로 줄이되 stop에는 즉시 반응한다.
        if stop_event is None:
            await asyncio.sleep(idle_delay)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=idle_delay)
            except asyncio.TimeoutError:
                pass
        idle_delay = min(config.WORKER_IDLE_MAX_SEC, idle_delay * 2)
