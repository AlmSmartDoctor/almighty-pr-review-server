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
