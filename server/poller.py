import asyncio

from server.github.gh import GhClient
from server.repos import pr_repo, repo_repo


def poll_once(conn, *, list_prs, enqueue) -> None:
    """enabled 레포별로 open PR을 upsert하고 새 head sha면 enqueue."""
    for repo in repo_repo.list_enabled(conn):
        if repo["trigger_mode"] != "auto":
            continue
        # ★개정 (codex v6/v7 [MEDIUM]): PR 발견·upsert·오버뷰·last_polled_at은
        # 항상 수행하고, **enqueue만** 벤더 유무로 가드한다. (벤더 0개 레포도
        # PR은 오버뷰에 뜨되 리뷰 job은 안 쌓임 → 재감지 루프 차단 + 발견 유지.
        # 나중에 벤더를 켜면 needs_review가 여전히 true라 다음 폴링에 enqueue됨)
        has_vendor = repo["vendor_claude_on"] or repo["vendor_codex_on"]
        for pr in list_prs(repo["full_name"]):
            pid = pr_repo.upsert(
                conn,
                repo_id=repo["id"],
                number=pr.number,
                title=pr.title,
                author=pr.author,
                head_sha=pr.head_sha,
                base_ref=pr.base_ref,
                url=pr.url,
                state=pr.state,
                created_at=pr.created_at,
            )
            if has_vendor and pr_repo.needs_review(conn, pid):
                enqueue(pid)
        repo_repo.update(conn, repo["id"], last_polled_at=_now(conn))


def _now(conn):
    return conn.execute("SELECT datetime('now') AS n").fetchone()["n"]


async def poll_loop(db_path, *, interval_sec: int = 60, stop_event=None):
    """★개정: 폴러는 매 틱 자기 커넥션을 열고, 새 head sha면 review_job enqueue."""
    from server.db import connect
    from server.repos import job_repo

    client = GhClient()
    while stop_event is None or not stop_event.is_set():
        conn = connect(db_path)
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
            except Exception as e:  # 한 틱의 실패가 폴러를 영구히 죽이지 않게
                print(f"[poller] tick failed: {e!r}")
        finally:
            conn.close()
        # ★개정: interval 대기 중에도 stop_event에 즉시 반응(graceful shutdown)
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(interval_sec)
