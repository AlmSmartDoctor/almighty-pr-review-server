import asyncio

from server import config
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
        for pr in open_prs:
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
                head_ref=pr.head_ref,
                body=pr.body,
            )
            open_numbers.append(pr.number)
            if has_vendor and pr_repo.needs_review(conn, pid):
                enqueue(pid)
        # 병합/닫힌 PR 재조정: 이 폴 이전에 열려 있었으나 gh 오픈 목록에서 사라진 것만
        # closed로. 목록이 상한에 걸려 잘렸으면(len==limit) 불완전한 셋이라 skip.
        if len(open_prs) < config.POLL_OPEN_PR_LIMIT:
            pr_repo.mark_closed(conn, repo["id"], prev_open - set(open_numbers))
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
