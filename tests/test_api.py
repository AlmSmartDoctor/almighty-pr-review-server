from fastapi.testclient import TestClient

from server.api import app, get_conn
from server.db import connect, init_schema


def _client(tmp_path):
    conn = connect(tmp_path / "api.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    return TestClient(app), conn


def test_add_and_list_repos(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/api/repos", json={"full_name": "acme/api"})
    assert r.status_code == 201
    lst = client.get("/api/repos").json()
    assert lst[0]["full_name"] == "acme/api"


def test_get_settings(tmp_path):
    client, _ = _client(tmp_path)
    s = client.get("/api/settings").json()
    assert s["concurrency_limit"] == 2


def test_update_finding_status(tmp_path):
    client, conn = _client(tmp_path)
    from server.repos import repo_repo, pr_repo, finding_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=1,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    fid = finding_repo.add(
        conn,
        run_id=run_id,
        vendor="claude",
        file="a",
        line=1,
        severity="high",
        category="bug",
        claim="c",
        rationale="r",
        confidence=0.8,
    )
    r = client.patch(f"/api/findings/{fid}", json={"status": "approved"})
    assert r.status_code == 200
    assert finding_repo.get(conn, fid)["status"] == "approved"


def test_patch_status_only_preserves_edited_text(tmp_path):
    """status-only PATCH가 기존 edited_text를 NULL로 덮지 않아야 한다(데이터 손실 방지)."""
    client, conn = _client(tmp_path)
    from server.repos import repo_repo, pr_repo, finding_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=2,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    fid = finding_repo.add(
        conn,
        run_id=run_id,
        vendor="claude",
        file="a",
        line=1,
        severity="high",
        category="bug",
        claim="c",
        rationale="r",
        confidence=0.8,
    )
    client.patch(
        f"/api/findings/{fid}",
        json={"status": "edited", "edited_text": "fixed wording"},
    )
    r = client.patch(f"/api/findings/{fid}", json={"status": "approved"})
    assert r.status_code == 200
    row = finding_repo.get(conn, fid)
    assert row["status"] == "approved"
    assert row["edited_text"] == "fixed wording"
