import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ALMIGHTY_E2E") != "1",
    reason="E2E는 실제 gh/claude/codex 인증 필요 — ALMIGHTY_E2E=1로 opt-in",
)


def test_end_to_end_single_pr(tmp_path):
    """실제 레포/PR 1건을 폴링→enqueue→worker 리뷰→findings 저장까지. 포스팅 X."""
    import asyncio

    from server.db import connect, init_schema
    from server.repos import repo_repo, pr_repo, job_repo, finding_repo
    from server.poller import poll_once
    from server.worker import run_one_job
    from server.github.gh import GhClient

    conn = connect(tmp_path / "e2e.db")
    init_schema(conn)
    repo_full = os.environ["ALMIGHTY_E2E_REPO"]  # 예: "me/sandbox"
    local = os.environ["ALMIGHTY_E2E_LOCAL"]  # 로컬 clone 경로
    repo_repo.add(conn, full_name=repo_full, local_path=local)
    gh = GhClient()

    def enqueue(pid):
        pr = pr_repo.get(conn, pid)
        job_repo.enqueue(conn, pr_id=pid, head_sha=pr["head_sha"], trigger="auto")

    poll_once(conn, list_prs=gh.list_open_prs, enqueue=enqueue)
    assert conn.execute("SELECT COUNT(*) c FROM review_job").fetchone()["c"] > 0

    # worker가 잡 1건을 끝까지 실행(실제 claude/codex 왕복)
    asyncio.run(run_one_job(conn, worker_id="e2e"))
    done = conn.execute(
        "SELECT * FROM review_job WHERE status IN ('done','failed') LIMIT 1"
    ).fetchone()
    assert done is not None
    if done["status"] == "done":
        assert finding_repo.list_for_run(conn, done["run_id"]) is not None
