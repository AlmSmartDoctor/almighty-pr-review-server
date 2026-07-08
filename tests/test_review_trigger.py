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
