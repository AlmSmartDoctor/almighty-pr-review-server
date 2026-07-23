import pytest
from fastapi.testclient import TestClient

from server.api import app, get_conn
from server.db import connect, init_schema
from server.repos import repo_repo, pr_repo


def test_manual_trigger_enqueues_job(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)
    rid = repo_repo.add(conn, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=3,
        title="t",
        author="a",
        head_sha="s3",
        base_ref="main",
        url="u",
    )
    r = client.post(f"/api/prs/{pid}/review")
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    row = conn.execute("SELECT * FROM review_job WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "queued" and row["trigger"] == "manual"
    app.dependency_overrides.clear()


@pytest.mark.parametrize("blocked", ["closed", "disabled", "no_vendor"])
def test_manual_trigger_rejects_unreviewable_pr_without_creating_job(tmp_path, blocked):
    conn = connect(tmp_path / "blocked.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)
    rid = repo_repo.add(conn, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=9,
        title="t",
        author="a",
        head_sha="s9",
        base_ref="main",
        url="u",
    )
    if blocked == "closed":
        pr_repo.mark_closed(conn, rid, {9})
    elif blocked == "disabled":
        repo_repo.update(conn, rid, enabled=0)
    else:
        repo_repo.update(conn, rid, vendor_claude_on=0, vendor_codex_on=0)

    response = client.post(f"/api/prs/{pid}/review")

    assert response.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM review_job").fetchone()[0] == 0
    app.dependency_overrides.clear()
    conn.close()


def test_manual_trigger_returns_conflict_while_retry_job_is_active(tmp_path):
    from server.repos import job_repo, review_repo

    conn = connect(tmp_path / "retry-conflict.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)
    rid = repo_repo.add(conn, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=10,
        title="t",
        author="a",
        head_sha="s10",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s10", trigger="manual", effort="medium"
    )
    job_id = job_repo.enqueue_retry(
        conn, pr_id=pid, head_sha="s10", run_id=run_id
    )

    response = client.post(f"/api/prs/{pid}/review")

    assert response.status_code == 409
    job = conn.execute(
        "SELECT trigger, retry_run_id FROM review_job WHERE id=?", (job_id,)
    ).fetchone()
    assert job["trigger"] == "retry"
    assert job["retry_run_id"] == run_id
    app.dependency_overrides.clear()
    conn.close()


def test_manual_retrigger_reopens_finished_job(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)
    rid = repo_repo.add(conn, full_name="acme/api", local_path="/tmp/acme")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=4,
        title="t",
        author="a",
        head_sha="s4",
        base_ref="main",
        url="u",
    )
    r1 = client.post(f"/api/prs/{pid}/review")
    assert r1.status_code == 202
    job_id = r1.json()["job_id"]
    conn.execute("UPDATE review_job SET status='done' WHERE id=?", (job_id,))
    conn.commit()

    r2 = client.post(f"/api/prs/{pid}/review")  # 같은 sha 재트리거
    assert r2.status_code == 202
    assert r2.json()["job_id"] == job_id  # 같은 잡 재개
    row = conn.execute("SELECT * FROM review_job WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "queued"  # 종료된 잡을 재리뷰 위해 다시 queued
    app.dependency_overrides.clear()
