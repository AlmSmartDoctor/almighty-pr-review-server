import pytest
from fastapi.testclient import TestClient

from server.api import app, get_conn
from server.db import connect, init_schema


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


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


def test_patch_repo_updates_local_path_and_enabled(tmp_path):
    client, _ = _client(tmp_path)
    created = client.post(
        "/api/repos",
        json={"full_name": "acme/api", "local_path": "/tmp/acme-api"},
    ).json()

    r = client.patch(
        f"/api/repos/{created['id']}",
        json={"enabled": 0, "local_path": "/tmp/acme-api-renamed"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] == 0
    assert body["local_path"] == "/tmp/acme-api-renamed"


def test_get_settings(tmp_path):
    client, _ = _client(tmp_path)
    s = client.get("/api/settings").json()
    assert s["concurrency_limit"] == 2


def test_patch_settings_context_toggles(tmp_path):
    client, _ = _client(tmp_path)
    r = client.patch(
        "/api/settings", json={"context_static_on": 1, "context_jira_on": 1}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["context_static_on"] == 1 and body["context_jira_on"] == 1


def test_patch_repo_context_settings(tmp_path):
    client, _ = _client(tmp_path)
    created = client.post("/api/repos", json={"full_name": "acme/api"}).json()
    r = client.patch(
        f"/api/repos/{created['id']}",
        json={
            "context_static_on": 1,
            "static_context_path": "/x/ctx.md",
            "jira_project_keys": "PROJ",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["context_static_on"] == 1
    assert body["static_context_path"] == "/x/ctx.md"
    assert body["jira_project_keys"] == "PROJ"


def test_patch_verify_singles_toggle(tmp_path):
    client, _ = _client(tmp_path)
    assert (
        client.patch("/api/settings", json={"verify_singles_on": 1}).json()[
            "verify_singles_on"
        ]
        == 1
    )
    created = client.post("/api/repos", json={"full_name": "acme/api"}).json()
    assert (
        client.patch(
            f"/api/repos/{created['id']}", json={"verify_singles_on": 0}
        ).json()["verify_singles_on"]
        == 0
    )
    # None으로 상속 복원
    assert (
        client.patch(
            f"/api/repos/{created['id']}", json={"verify_singles_on": None}
        ).json()["verify_singles_on"]
        is None
    )


def test_patch_incremental_review_toggle(tmp_path):
    client, _ = _client(tmp_path)
    assert (
        client.patch("/api/settings", json={"incremental_review_on": 1}).json()[
            "incremental_review_on"
        ]
        == 1
    )
    created = client.post("/api/repos", json={"full_name": "acme/api"}).json()
    assert (
        client.patch(
            f"/api/repos/{created['id']}", json={"incremental_review_on": 0}
        ).json()["incremental_review_on"]
        == 0
    )
    assert (
        client.patch(
            f"/api/repos/{created['id']}", json={"incremental_review_on": None}
        ).json()["incremental_review_on"]
        is None
    )


def test_patch_repo_can_restore_context_toggle_inheritance(tmp_path):
    client, _ = _client(tmp_path)
    created = client.post("/api/repos", json={"full_name": "acme/api"}).json()
    client.patch(
        f"/api/repos/{created['id']}", json={"context_jira_on": 0}
    ).raise_for_status()

    r = client.patch(f"/api/repos/{created['id']}", json={"context_jira_on": None})

    assert r.status_code == 200
    assert r.json()["context_jira_on"] is None


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


def test_run_context_returns_text_and_meta(tmp_path):
    client, conn = _client(tmp_path)
    from server.repos import repo_repo, pr_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=30,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    review_repo.set_context(conn, run_id, text="ctx", meta={"sources": []})
    r = client.get(f"/api/runs/{run_id}/context")
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "ctx" and body["meta"] == {"sources": []}


def test_run_context_404_for_missing_run(tmp_path):
    client, _ = _client(tmp_path)
    r = client.get("/api/runs/99999/context")
    assert r.status_code == 404


def test_run_context_empty_when_unstored(tmp_path):
    client, conn = _client(tmp_path)
    from server.repos import repo_repo, pr_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=31,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    r = client.get(f"/api/runs/{run_id}/context")
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "" and body["meta"] is None


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


def test_run_context_endpoint_redacts_secret_across_sinks(tmp_path, monkeypatch):
    import asyncio
    from contextlib import contextmanager
    from server import config
    from server.context.base import ContextResult
    from server.models import Finding
    from server.pipeline import review_pr, PipelineDeps
    from server.repos import repo_repo, pr_repo, review_repo

    monkeypatch.setattr(config, "JIRA_API_TOKEN", "tok-LEAK")
    client, conn = _client(tmp_path)

    @contextmanager
    def fake_wt(repo, sha, pr_number=None):
        yield "/tmp/fake-wt"

    class OneAdapter:
        vendor = "claude"

        async def review(self, **kw):
            return [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]

    class DirectErrCtx:
        def __init__(self):
            self.results = [
                ContextResult(
                    provider="jira",
                    status="error",
                    error="auth failed with tok-LEAK in header",
                )
            ]

        def gather(self, *, req):
            return ""

    ctx = DirectErrCtx()
    rid = repo_repo.add(conn, full_name="acme/api", local_path="/tmp/x")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=50,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_wt,
        adapters=[OneAdapter()],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/x",
        context=ctx,
    )
    run_id = asyncio.run(review_pr(conn, pr_id=pid, trigger="manual", deps=deps))

    # sink 3: HTTP endpoint response
    r = client.get(f"/api/runs/{run_id}/context")
    assert r.status_code == 200
    assert "tok-LEAK" not in r.text
    # sink 2: persisted meta
    stored = review_repo.get_run(conn, run_id)
    assert "tok-LEAK" not in (stored["context_meta"] or "")
    assert "[redacted]" in stored["context_meta"]
