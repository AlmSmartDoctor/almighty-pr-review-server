from server.db import connect, init_schema
from server.repos import job_repo, pr_repo, repo_repo, review_repo
from tests.test_e2e_smoke import e2e_state_message


def test_e2e_reports_job_state_when_no_terminal_job(tmp_path):
    conn = connect(tmp_path / "e2e.db")
    init_schema(conn)
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=7,
        title="Add feature",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    job_repo.enqueue(conn, pr_id=pid, head_sha="s", trigger="auto")
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="auto", effort="medium"
    )
    review_repo.finish_run(conn, run_id, "canceled", error="diff too large")
    conn.execute(
        "UPDATE review_job SET status='queued', error=?, next_run_at=? WHERE pr_id=?",
        ("waiting", "2099-01-01 00:00:00", pid),
    )
    conn.commit()

    msg = e2e_state_message(conn)

    assert "review_job rows=" in msg
    assert "review_run rows=" in msg
    assert "queued" in msg
    assert "diff too large" in msg
    assert "next_run_at" in msg
