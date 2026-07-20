import asyncio

from server import config
from server.context.base import redact_secrets
from server.context.registry import _effective
from server.github.gh import GhClient
from server.repos import pr_repo, repo_repo, settings_repo


def poll_once(conn, *, list_prs, enqueue) -> list[dict]:
    """enabled 레포별로 open PR을 upsert하고 새 head sha면 enqueue.
    레포 하나의 실패(gh 에러 등)가 뒤 레포 폴링을 막지 않게 per-repo 격리한다."""
    results = []
    for repo in repo_repo.list_enabled(conn):
        try:
            results.append(sync_repo(conn, repo, list_prs=list_prs, enqueue=enqueue))
        except Exception as exc:
            print(f"[poller] {repo['full_name']} poll failed: {exc!r}")
            results.append(
                {
                    "repo_id": repo["id"],
                    "repo": repo["full_name"],
                    "ok": False,
                    "error": _poll_error(exc),
                }
            )
    return results


def sync_repo(conn, repo, *, list_prs, enqueue) -> dict:
    """레포 한 곳을 동기화하고 성공·실패 상태를 DB에 남긴다."""
    settings = settings_repo.get(conn)
    try:
        return _poll_repo(
            conn, repo, settings, list_prs=list_prs, enqueue=enqueue
        )
    except Exception as exc:
        message = _poll_error(exc)
        repo_repo.update(conn, repo["id"], last_poll_error=message)
        raise


def _poll_error(exc: Exception) -> str:
    return redact_secrets(f"{type(exc).__name__}: {exc}")[:1000]


def _poll_repo(conn, repo, settings, *, list_prs, enqueue) -> dict:
    # ★개정: PR 발견·upsert·오버뷰·재조정·last_polled_at은 모든 enabled 레포에서
    # 항상 수행하고, **enqueue(자동 리뷰)만** trigger_mode/벤더로 가드한다. manual
    # 레포도 오픈 PR이 대시보드에 발견돼 사람이 리뷰 버튼으로 트리거할 수 있고(수동
    # 트리거는 upsert된 pr_id가 필요), 벤더 0개면 job만 안 쌓인다(재감지 루프 차단).
    auto = repo["trigger_mode"] == "auto"
    has_vendor = repo["vendor_claude_on"] or repo["vendor_codex_on"]
    skip_draft = _effective(repo, settings, "skip_draft_on")
    # 이 폴 이전에 DB가 열림으로 알던 PR 집합(list_prs 호출 전에 캡처) — 폴 도중
    # 웹훅 등으로 삽입된 PR은 여기에 없어 재조정 대상에서 자연히 제외된다.
    prev_open = {
        r["number"]
        for r in conn.execute(
            "SELECT number FROM pull_request WHERE repo_id=? AND state='open'",
            (repo["id"],),
        ).fetchall()
    }
    open_prs = list_prs(repo["full_name"])
    open_numbers = []
    enqueued_jobs = 0
    for pr in open_prs:
        pid = pr_repo.upsert(
            conn,
            repo_id=repo["id"],
            number=pr.number,
            title=pr.title,
            author=pr.author,
            head_sha=pr.head_sha,
            base_ref=pr.base_ref,
            base_sha=pr.base_sha,
            url=pr.url,
            state=pr.state,
            created_at=pr.created_at,
            head_ref=pr.head_ref,
            body=pr.body,
            is_draft=pr.is_draft,
        )
        open_numbers.append(pr.number)
        if (
            auto
            and has_vendor
            and not (skip_draft and pr.is_draft)
            and pr_repo.needs_review(conn, pid)
        ):
            result = enqueue(pid)
            if result is not False:
                enqueued_jobs += 1
    # 병합/닫힌 PR 재조정: 이 폴 이전에 열려 있었으나 gh 오픈 목록에서 사라진 것만
    # closed로. 목록이 상한에 걸려 잘렸으면(len==limit) 불완전한 셋이라 skip.
    if len(open_prs) < config.POLL_OPEN_PR_LIMIT:
        pr_repo.mark_closed(conn, repo["id"], prev_open - set(open_numbers))
    polled_at = _now(conn)
    repo_repo.update(
        conn, repo["id"], last_polled_at=polled_at, last_poll_error=None
    )
    return {
        "repo_id": repo["id"],
        "repo": repo["full_name"],
        "ok": True,
        "open_prs": len(open_prs),
        "enqueued_jobs": enqueued_jobs,
        "last_polled_at": polled_at,
        "error": None,
    }


def _now(conn):
    return conn.execute("SELECT datetime('now') AS n").fetchone()["n"]


async def poll_loop(db_path, *, interval_sec: int = 60, stop_event=None):
    """★개정: 폴러는 매 틱 자기 커넥션을 열고, 새 head sha면 review_job enqueue.
    대기 간격은 매 틱 app_settings.default_poll_interval을 읽어 반영(웹 UI 수정이
    재시작 없이 적용). interval_sec 인자는 설정 조회 실패 시 폴백 겸 테스트 seam."""
    from server.db import connect
    from server.repos import job_repo, settings_repo

    client = GhClient()
    while stop_event is None or not stop_event.is_set():
        conn = connect(db_path)
        interval = interval_sec
        try:

            def enqueue(pid):
                pr = pr_repo.get(conn, pid)
                job_repo.enqueue(
                    conn, pr_id=pid, head_sha=pr["head_sha"], trigger="auto"
                )

            try:
                await asyncio.to_thread(
                    poll_once, conn, list_prs=client.list_open_prs, enqueue=enqueue
                )
                row = settings_repo.get(conn)
                if row and row["default_poll_interval"]:
                    interval = row["default_poll_interval"]
            except Exception as e:  # 한 틱의 실패가 폴러를 영구히 죽이지 않게
                print(f"[poller] tick failed: {e!r}")
        finally:
            conn.close()
        # ★개정: interval 대기 중에도 stop_event에 즉시 반응(graceful shutdown)
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(interval)
