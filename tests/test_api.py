import json

import pytest
from fastapi.testclient import TestClient

from server.api import app, get_conn, get_gh
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


def test_repo_readiness_endpoint_checks_registered_repo(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    rid = client.post("/api/repos", json={"full_name": "acme/api"}).json()["id"]

    class Gh:
        def preflight_repo(self, full_name):
            return {"full_name": full_name}

    app.dependency_overrides[get_gh] = lambda: Gh()
    monkeypatch.setattr("server.repo_readiness.list_harnesses", lambda: ["default"])
    monkeypatch.setattr(
        "server.repo_readiness.shutil.which", lambda command: f"/bin/{command}"
    )

    response = client.get(f"/api/repos/{rid}/readiness")

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert client.get("/api/repos/999/readiness").status_code == 404


def test_sync_repo_discovers_pr_and_does_not_duplicate_auto_job(tmp_path):
    from server.github.gh import PrInfo

    client, conn = _client(tmp_path)
    rid = client.post("/api/repos", json={"full_name": "acme/api"}).json()["id"]

    class Gh:
        def list_open_prs(self, full_name):
            return [PrInfo(7, "new", "kim", "sha1", "main", "url", "open")]

    app.dependency_overrides[get_gh] = lambda: Gh()

    first = client.post(f"/api/repos/{rid}/sync")
    second = client.post(f"/api/repos/{rid}/sync")

    assert first.status_code == 200
    assert first.json()["open_prs"] == 1
    assert first.json()["enqueued_jobs"] == 1
    assert second.json()["enqueued_jobs"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM review_job").fetchone()["n"] == 1
    assert client.get("/api/repos").json()[0]["open_pr_count"] == 1
    repo = conn.execute(
        "SELECT last_polled_at, last_poll_error FROM repo WHERE id=?", (rid,)
    ).fetchone()
    assert repo["last_polled_at"] is not None and repo["last_poll_error"] is None


def test_sync_repo_rejects_disabled_and_persists_failure(tmp_path):
    from server.repos import repo_repo

    client, conn = _client(tmp_path)
    rid = client.post("/api/repos", json={"full_name": "acme/api"}).json()["id"]

    class Gh:
        def list_open_prs(self, full_name):
            raise RuntimeError("network down")

    app.dependency_overrides[get_gh] = lambda: Gh()
    failed = client.post(f"/api/repos/{rid}/sync")
    assert failed.status_code == 502
    assert "network down" in failed.json()["detail"]
    assert "network down" in repo_repo.get(conn, rid)["last_poll_error"]

    client.patch(f"/api/repos/{rid}", json={"enabled": 0}).raise_for_status()
    assert client.post(f"/api/repos/{rid}/sync").status_code == 409


def test_sync_all_isolates_repository_failures(tmp_path):
    from server.github.gh import PrInfo
    from server.repos import repo_repo

    client, conn = _client(tmp_path)
    bad = client.post("/api/repos", json={"full_name": "acme/bad"}).json()["id"]
    good = client.post("/api/repos", json={"full_name": "acme/good"}).json()["id"]

    class Gh:
        def list_open_prs(self, full_name):
            if full_name == "acme/bad":
                raise RuntimeError("gh unavailable")
            return [PrInfo(8, "ok", "kim", "sha2", "main", "url", "open")]

    app.dependency_overrides[get_gh] = lambda: Gh()
    response = client.post("/api/repos/sync")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["repositories"] == 2
    assert response.json()["open_prs"] == 1
    assert repo_repo.get(conn, bad)["last_poll_error"] is not None
    assert repo_repo.get(conn, good)["last_polled_at"] is not None


def test_list_models_from_backend(tmp_path):
    client, _ = _client(tmp_path)
    m = client.get("/api/models").json()
    assert "gpt-5.6-sol" in m["codex"] and "sonnet" in m["claude"]
    assert m["claude_efforts"] and m["codex_efforts"]


def test_add_repo_normalizes_full_name(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post(
        "/api/repos", json={"full_name": "  https://github.com/acme/api.git/  "}
    )
    assert r.status_code == 201 and r.json()["full_name"] == "acme/api"


def test_add_repo_rejects_malformed_full_name(tmp_path):
    client, _ = _client(tmp_path)
    assert (
        client.post("/api/repos", json={"full_name": "not-a-repo"}).status_code == 400
    )


def test_overview_includes_pr_url_and_jira_links(tmp_path, monkeypatch):
    from server import config as cfg

    monkeypatch.setattr(cfg, "JIRA_BASE_URL", "https://jira.example.com/")
    client, conn = _client(tmp_path)
    from server.repos import pr_repo, repo_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pr_repo.upsert(
        conn,
        repo_id=rid,
        number=7,
        title="PROJ-42 버그 수정",
        author="a",
        head_sha="s",
        base_ref="main",
        url="https://github.com/acme/api/pull/7",
    )
    row = client.get("/api/overview").json()[0]
    assert row["url"] == "https://github.com/acme/api/pull/7"
    assert {
        "key": "PROJ-42",
        "url": "https://jira.example.com/browse/PROJ-42",
    } in row["jira_links"]
    assert "body" not in row  # 본문은 키 추출에만 쓰고 응답엔 싣지 않음


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


def test_context_status_reports_readiness_without_secret_values(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    first = client.post("/api/repos", json={"full_name": "acme/api"}).json()
    client.post("/api/repos", json={"full_name": "acme/web"})
    client.patch(
        "/api/settings",
        json={"context_static_on": 1, "context_jira_on": 1, "context_db_schema_on": 1},
    )
    # target은 DB가 꺼진 레포에만 있고, 실제 DB-on 레포에는 없다. configured count가
    # 전체 레포 target 수가 아니라 effective-on 교집합인지 검증한다.
    client.patch(
        f"/api/repos/{first['id']}",
        json={
            "context_static_on": 0,
            "context_db_schema_on": 0,
            "db_schema_path": "db/schema.sql",
        },
    )
    monkeypatch.setattr("server.config.JIRA_BASE_URL", "https://private-jira.example")
    monkeypatch.setattr("server.config.JIRA_EMAIL", "private@example.com")
    monkeypatch.setattr("server.config.JIRA_API_TOKEN", "")
    monkeypatch.setattr("server.config.MSSQL_GATEWAY_URL", "https://private-gateway.example")
    monkeypatch.setattr("server.config.MSSQL_GATEWAY_TOKEN", "")

    response = client.get("/api/settings/context-status")

    assert response.status_code == 200
    body = response.json()
    assert body["total_repos"] == 2
    static = body["sources"]["context_static_on"]
    assert static["enabled_repos"] == 1
    assert static["configured_repos"] == 1  # 기본 지침 자동 탐색은 별도 path 불필요
    assert static["explicit_path_repos"] == 0
    db_schema = body["sources"]["context_db_schema_on"]
    assert db_schema["enabled_repos"] == 1
    assert db_schema["configured_repos"] == 0
    jira = body["sources"]["context_jira_on"]
    assert jira["available"] is False
    assert jira["configured_repos"] == 0
    assert jira["missing"] == ["ALMIGHTY_JIRA_API_TOKEN"]
    live_db = body["sources"]["context_db_schema_on"]["capabilities"]["live_db"]
    assert live_db["available"] is False
    assert live_db["missing"] == ["ALMIGHTY_MSSQL_GATEWAY_TOKEN"]
    serialized = response.text
    assert "private-jira.example" not in serialized
    assert "private@example.com" not in serialized
    assert "private-gateway.example" not in serialized


def test_context_status_configured_counts_follow_effective_repo_overrides(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    first = client.post("/api/repos", json={"full_name": "acme/api"}).json()
    client.post("/api/repos", json={"full_name": "acme/web"})
    client.patch(
        "/api/settings",
        json={
            "context_jira_on": 1,
            "context_feedback_on": 1,
            "context_current_pr_reviews_on": 1,
        },
    )
    client.patch(
        f"/api/repos/{first['id']}",
        json={
            "context_jira_on": 0,
            "context_feedback_on": 0,
            "context_current_pr_reviews_on": 0,
        },
    )
    monkeypatch.setattr("server.config.JIRA_BASE_URL", "https://jira.example")
    monkeypatch.setattr("server.config.JIRA_EMAIL", "bot@example.com")
    monkeypatch.setattr("server.config.JIRA_API_TOKEN", "secret")

    sources = client.get("/api/settings/context-status").json()["sources"]

    for key in (
        "context_jira_on",
        "context_feedback_on",
        "context_current_pr_reviews_on",
    ):
        assert sources[key]["enabled_repos"] == 1
        assert sources[key]["configured_repos"] == 1


def test_patch_settings_context_toggles(tmp_path):
    client, _ = _client(tmp_path)
    r = client.patch(
        "/api/settings", json={"context_static_on": 1, "context_jira_on": 1}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["context_static_on"] == 1 and body["context_jira_on"] == 1


def test_patch_current_pr_reviews_context_globally_and_per_repo(tmp_path):
    client, _ = _client(tmp_path)
    repo = client.post("/api/repos", json={"full_name": "acme/api"}).json()

    settings = client.patch(
        "/api/settings", json={"context_current_pr_reviews_on": 1}
    )
    override = client.patch(
        f"/api/repos/{repo['id']}", json={"context_current_pr_reviews_on": 0}
    )

    assert settings.status_code == 200
    assert settings.json()["context_current_pr_reviews_on"] == 1
    assert override.status_code == 200
    assert override.json()["context_current_pr_reviews_on"] == 0


def test_patch_settings_rejects_non_claude_prescreen_model(tmp_path):
    client, _ = _client(tmp_path)

    for model in ("gpt-5.6-terra", "gemini-2.5-pro", "llama-3", "claude-haiku"):
        invalid = client.patch("/api/settings", json={"prescreen_model": model})
        assert invalid.status_code == 400
        assert "Claude 모델" in invalid.json()["detail"]

    valid = client.patch("/api/settings", json={"prescreen_model": "claude-future-1"})
    assert valid.status_code == 200
    assert valid.json()["prescreen_model"] == "claude-future-1"


def test_patch_settings_rejects_invalid_threshold(tmp_path):
    # 임의 문자열이 저장되면 decide()가 KeyError로 죽어 이후 모든 리뷰가 실패한다.
    client, _ = _client(tmp_path)
    r = client.patch("/api/settings", json={"prescreen_gate_threshold": "extreme"})
    assert r.status_code == 400
    assert client.get("/api/settings").json()["prescreen_gate_threshold"] != "extreme"
    ok = client.patch("/api/settings", json={"prescreen_gate_threshold": "complex"})
    assert ok.status_code == 200
    assert ok.json()["prescreen_gate_threshold"] == "complex"


@pytest.mark.parametrize(
    "patch",
    [
        {"concurrency_limit": 0},
        {"concurrency_limit": 9},
        {"default_poll_interval": 14},
        {"default_poll_interval": 86_401},
        {"context_static_on": 2},
        {"verify_singles_on": -1},
        {"default_effort": "max"},
        {"claude_effort": "minimal"},
        {"codex_effort": "max"},
    ],
)
def test_patch_settings_rejects_values_that_can_break_background_work(tmp_path, patch):
    client, _ = _client(tmp_path)
    before = client.get("/api/settings").json()

    response = client.patch("/api/settings", json=patch)

    assert response.status_code == 400
    assert client.get("/api/settings").json() == before


def test_patch_settings_accepts_safe_boundaries_and_vendor_efforts(tmp_path):
    client, _ = _client(tmp_path)

    response = client.patch(
        "/api/settings",
        json={
            "concurrency_limit": 1,
            "default_poll_interval": 15,
            "context_static_on": 1,
            "verify_singles_on": 0,
            "default_effort": "xhigh",
            "claude_effort": "max",
            "codex_effort": "minimal",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["concurrency_limit"] == 1
    assert body["default_poll_interval"] == 15
    assert body["claude_effort"] == "max"
    assert body["codex_effort"] == "minimal"

    maximum = client.patch("/api/settings", json={"default_poll_interval": 86_400})
    assert maximum.status_code == 200
    assert maximum.json()["default_poll_interval"] == 86_400


def test_add_repo_duplicate_returns_409(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/repos", json={"full_name": "acme/api"}).status_code == 201
    assert client.post("/api/repos", json={"full_name": "acme/api"}).status_code == 409


def test_patch_repo_renames_and_validates_repository(tmp_path):
    client, _ = _client(tmp_path)
    first = client.post("/api/repos", json={"full_name": "acme/api"}).json()
    client.post("/api/repos", json={"full_name": "acme/web"}).raise_for_status()

    renamed = client.patch(
        f"/api/repos/{first['id']}",
        json={"full_name": "https://github.com/acme/backend.git"},
    )

    assert renamed.status_code == 200
    assert renamed.json()["full_name"] == "acme/backend"
    assert client.patch(f"/api/repos/{first['id']}", json={"full_name": "bad"}).status_code == 400
    assert client.patch(f"/api/repos/{first['id']}", json={"full_name": "ACME/WEB"}).status_code == 409
    assert client.patch("/api/repos/999", json={"enabled": 0}).status_code == 404
    assert client.patch(f"/api/repos/{first['id']}", json={"enabled": 2}).status_code == 400


def test_patch_repo_rejects_invalid_vendor_efforts_without_mutating_repo(tmp_path):
    client, _ = _client(tmp_path)
    repo = client.post("/api/repos", json={"full_name": "acme/api"}).json()

    invalid_claude = client.patch(
        f"/api/repos/{repo['id']}", json={"claude_effort": "minimal"}
    )
    invalid_codex = client.patch(
        f"/api/repos/{repo['id']}", json={"codex_effort": "max"}
    )

    assert invalid_claude.status_code == 400
    assert invalid_codex.status_code == 400
    current = next(
        item for item in client.get("/api/repos").json() if item["id"] == repo["id"]
    )
    assert current["claude_effort"] is None
    assert current["codex_effort"] is None

    valid = client.patch(
        f"/api/repos/{repo['id']}",
        json={"claude_effort": "max", "codex_effort": "minimal"},
    )
    assert valid.status_code == 200
    assert valid.json()["claude_effort"] == "max"
    assert valid.json()["codex_effort"] == "minimal"


def test_delete_repo_removes_all_dependent_data(tmp_path):
    client, conn = _client(tmp_path)
    rid = client.post("/api/repos", json={"full_name": "acme/api"}).json()["id"]
    pr_id = conn.execute(
        "INSERT INTO pull_request (repo_id, number, head_sha) VALUES (?, 1, 'sha')",
        (rid,),
    ).lastrowid
    conn.execute(
        "INSERT INTO pre_screen (pr_id, head_sha, created_at) VALUES (?, 'sha', datetime('now'))",
        (pr_id,),
    )
    run_id = conn.execute(
        "INSERT INTO review_run (pr_id, head_sha, status) VALUES (?, 'sha', 'done')",
        (pr_id,),
    ).lastrowid
    vendor_id = conn.execute(
        "INSERT INTO vendor_result (run_id, vendor, status) VALUES (?, 'claude', 'done')",
        (run_id,),
    ).lastrowid
    finding_id = conn.execute(
        "INSERT INTO finding (run_id, vendor_result_id, vendor, status) VALUES (?, ?, 'claude', 'approved')",
        (run_id, vendor_id),
    ).lastrowid
    operation_id = conn.execute(
        """INSERT INTO github_post_operation
           (operation_key, run_id, vendor, marker, body, finding_ids,
            new_finding_ids, status, created_at, updated_at)
           VALUES ('op-key', ?, 'claude', 'marker', 'body', ?, ?,
                   'completed', datetime('now'), datetime('now'))""",
        (run_id, f"[{finding_id}]", f"[{finding_id}]"),
    ).lastrowid
    conn.execute(
        "UPDATE finding SET posting_operation_id=? WHERE id=?",
        (operation_id, finding_id),
    )
    conn.execute(
        "INSERT INTO finding_decision (finding_id, to_status, decided_at) VALUES (?, 'approved', datetime('now'))",
        (finding_id,),
    )
    conn.execute(
        "INSERT INTO posted_comment (run_id, vendor) VALUES (?, 'claude')", (run_id,)
    )
    conn.execute(
        "INSERT INTO slack_post (run_id, channel, ts, posted_at) VALUES (?, 'c', '1', datetime('now'))",
        (run_id,),
    )
    conn.execute(
        "INSERT INTO feedback_signal (run_id, source, slack_user, reaction, verdict, created_at) VALUES (?, 'slack', 'u', 'thumbsup', 'positive', datetime('now'))",
        (run_id,),
    )
    conn.execute(
        "INSERT INTO review_job (pr_id, head_sha, status) VALUES (?, 'sha', 'done')",
        (pr_id,),
    )
    conn.execute(
        "INSERT INTO wiki_page (repo_id, status, updated_at) VALUES (?, 'ready', datetime('now'))",
        (rid,),
    )
    conn.commit()

    response = client.delete(f"/api/repos/{rid}")

    assert response.status_code == 200
    assert client.get("/api/repos").json() == []
    for table in (
        "pull_request", "pre_screen", "review_run", "vendor_result", "finding",
        "finding_decision", "posted_comment", "slack_post", "feedback_signal",
        "review_job", "wiki_page", "github_post_operation",
    ):
        assert conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"] == 0


def test_delete_repo_rejects_active_work_and_missing_repo(tmp_path):
    client, conn = _client(tmp_path)
    rid = client.post("/api/repos", json={"full_name": "acme/api"}).json()["id"]
    pr_id = conn.execute(
        "INSERT INTO pull_request (repo_id, number, head_sha) VALUES (?, 1, 'sha')",
        (rid,),
    ).lastrowid
    conn.execute(
        "INSERT INTO review_job (pr_id, head_sha, status) VALUES (?, 'sha', 'running')",
        (pr_id,),
    )
    conn.commit()

    assert client.delete(f"/api/repos/{rid}").status_code == 409
    assert (
        client.patch(f"/api/repos/{rid}", json={"full_name": "acme/backend"}).status_code
        == 409
    )
    assert client.delete("/api/repos/999").status_code == 404


def test_post_endpoints_missing_ids_return_404(tmp_path):
    client, _ = _client(tmp_path)

    class NeverGh:
        def __getattr__(self, name):
            raise AssertionError("gh must not be called for missing ids")

    from server.api import get_gh

    app.dependency_overrides[get_gh] = lambda: NeverGh()
    assert client.post("/api/runs/999/post").status_code == 404
    assert client.get("/api/prs/999/post-health").status_code == 404


def test_patch_repo_context_settings(tmp_path):
    client, _ = _client(tmp_path)
    created = client.post("/api/repos", json={"full_name": "acme/api"}).json()
    r = client.patch(
        f"/api/repos/{created['id']}",
        json={
            "context_static_on": 1,
            "context_feedback_on": 1,
            "static_context_path": "/x/ctx.md",
            "jira_project_keys": "PROJ",
            "db_schema_path": "db/structure.sql",
            "live_db_target_id": " tenant-7 ",
            "graphify_path": "docs/PROJECT.md",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["context_static_on"] == 1
    assert body["context_feedback_on"] == 1
    assert body["static_context_path"] == "/x/ctx.md"
    assert body["jira_project_keys"] == "PROJ"
    assert body["db_schema_path"] == "db/structure.sql"
    assert body["live_db_target_id"] == "tenant-7"
    assert body["graphify_path"] == "docs/PROJECT.md"


def test_patch_repo_rejects_unsafe_live_db_target_id(tmp_path):
    client, _ = _client(tmp_path)
    created = client.post("/api/repos", json={"full_name": "acme/api"}).json()

    response = client.patch(
        f"/api/repos/{created['id']}",
        json={"live_db_target_id": "tenant/7?secret=x"},
    )

    assert response.status_code == 400


def _seed_learn_decisions(conn, full_name, decisions):
    from server.repos import finding_repo, repo_repo

    rid = repo_repo.add(conn, full_name=full_name)
    pr = conn.execute(
        "INSERT INTO pull_request (repo_id, number, head_sha) VALUES (?, 1, 's')",
        (rid,),
    ).lastrowid
    run = conn.execute(
        "INSERT INTO review_run (pr_id, head_sha) VALUES (?, 's')", (pr,)
    ).lastrowid
    conn.commit()
    for cat, status, claim in decisions:
        fid = finding_repo.add(
            conn,
            run_id=run,
            vendor="claude",
            file="a.py",
            line=1,
            severity="high",
            category=cat,
            claim=claim,
            rationale="r",
            confidence=0.9,
        )
        finding_repo.set_status(conn, fid, status)


def test_learn_returns_repo_feedback_and_excludes_empty(tmp_path):
    from server.repos import repo_repo

    client, conn = _client(tmp_path)
    _seed_learn_decisions(
        conn,
        "acme/api",
        [
            ("style", "dismissed", "nit A"),
            ("style", "dismissed", "nit B"),
            ("correctness", "approved", "real bug"),
        ],
    )
    repo_repo.add(conn, full_name="empty/repo")  # 사람 결정 없음 → 제외

    body = client.get("/api/learn").json()

    assert [r["repo"] for r in body] == ["acme/api"]  # 결정 없는 레포 제외
    entry = body[0]
    assert entry["total"] == 3
    cats = {c["category"]: c for c in entry["categories"]}
    assert cats["style"]["rejected"] == 2
    assert cats["correctness"]["approved"] == 1
    assert {e["claim"] for e in entry["rejected_examples"]} == {"nit A", "nit B"}


def test_learn_orders_repos_by_decision_count(tmp_path):
    client, conn = _client(tmp_path)
    _seed_learn_decisions(  # total 2, 이름은 앞서지만 결정 수는 적음
        conn, "acme/api", [("style", "dismissed", "a"), ("style", "dismissed", "b")]
    )
    _seed_learn_decisions(  # total 3
        conn,
        "acme/zzz",
        [
            ("perf", "approved", "c"),
            ("perf", "approved", "d"),
            ("correctness", "approved", "e"),
        ],
    )

    body = client.get("/api/learn").json()

    # 이름순이 아니라 결정 수 많은 순 — zzz(3)가 api(2)보다 앞
    assert [r["repo"] for r in body] == ["acme/zzz", "acme/api"]


def test_learn_includes_recent_decisions(tmp_path):
    client, conn = _client(tmp_path)
    _seed_learn_decisions(
        conn,
        "acme/api",
        [("style", "dismissed", "기각 지적"), ("correctness", "approved", "승인 지적")],
    )

    entry = client.get("/api/learn").json()[0]

    claims = [d["claim"] for d in entry["recent_decisions"]]
    assert "기각 지적" in claims and "승인 지적" in claims
    assert all(
        {"verdict", "decided_at", "pr_number", "category"} <= d.keys()
        for d in entry["recent_decisions"]
    )


def test_learn_empty_without_decisions(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/learn").json() == []


def test_review_rule_proposal_requires_approval_and_is_exposed_by_learn(tmp_path):
    client, conn = _client(tmp_path)
    _seed_learn_decisions(
        conn,
        "acme/api",
        [
            ("style", "dismissed", "nit A"),
            ("style", "dismissed", "nit B"),
            ("style", "dismissed", "nit C"),
        ],
    )
    rid = conn.execute(
        "SELECT id FROM repo WHERE full_name='acme/api'"
    ).fetchone()["id"]

    proposed = client.post(f"/api/repos/{rid}/review-rules/propose")
    assert proposed.status_code == 200
    rule = proposed.json()[0]
    assert rule["status"] == "proposed"

    learn_entry = client.get("/api/learn").json()[0]
    assert learn_entry["repo_id"] == rid
    assert learn_entry["review_rules"][0]["id"] == rule["id"]

    activated = client.patch(
        f"/api/review-rules/{rule['id']}", json={"status": "active"}
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"

    disabled = client.patch(
        f"/api/review-rules/{rule['id']}", json={"status": "disabled"}
    )
    assert disabled.json()["status"] == "disabled"


def test_review_rule_endpoints_validate_repo_rule_and_status(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/repos/999/review-rules/propose").status_code == 404
    assert client.patch(
        "/api/review-rules/999", json={"status": "active"}
    ).status_code == 404
    assert client.patch(
        "/api/review-rules/999", json={"status": "proposed"}
    ).status_code == 400


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


def test_new_repo_leaves_model_effort_null_to_inherit_global(tmp_path):
    # 새 레포는 모델/effort를 seed하지 않는다 — NULL이면 리뷰 시 전역 기본값을 상속한다.
    client, _ = _client(tmp_path)
    created = client.post("/api/repos", json={"full_name": "acme/api"}).json()
    assert created["claude_model"] is None
    assert created["claude_effort"] is None
    assert created["codex_model"] is None
    assert created["codex_effort"] is None


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


def test_patch_repo_can_restore_feedback_toggle_inheritance(tmp_path):
    client, _ = _client(tmp_path)
    created = client.post("/api/repos", json={"full_name": "acme/api"}).json()
    client.patch(
        f"/api/repos/{created['id']}", json={"context_feedback_on": 0}
    ).raise_for_status()

    r = client.patch(f"/api/repos/{created['id']}", json={"context_feedback_on": None})

    assert r.status_code == 200
    assert r.json()["context_feedback_on"] is None  # None-reset 루프에 포함


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


def test_update_finding_rejects_invalid_contracts_without_mutation(tmp_path):
    client, conn = _client(tmp_path)
    from server.repos import finding_repo, pr_repo, repo_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=3,
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

    invalid_requests = [
        {"status": "garbage"},
        {"status": "edited"},
        {"status": "edited", "edited_text": "   "},
        {"status": "approved", "edited_text": "unexpected rewrite"},
    ]
    for payload in invalid_requests:
        response = client.patch(f"/api/findings/{fid}", json=payload)
        assert response.status_code == 400
        assert finding_repo.get(conn, fid)["status"] == "pending"

    assert client.patch(
        "/api/findings/999999", json={"status": "approved"}
    ).status_code == 404

    edited = client.patch(
        f"/api/findings/{fid}",
        json={"status": "edited", "edited_text": "  fixed wording  "},
    )
    assert edited.status_code == 200
    assert edited.json()["edited_text"] == "fixed wording"

    conn.execute("UPDATE pull_request SET head_sha='new' WHERE id=?", (pid,))
    conn.commit()
    before_new_run = client.patch(
        f"/api/findings/{fid}", json={"status": "approved"}
    )
    assert before_new_run.status_code == 409
    assert finding_repo.get(conn, fid)["status"] == "edited"

    review_repo.create_run(
        conn, pr_id=pid, head_sha="new", trigger="manual", effort="medium"
    )
    stale = client.patch(f"/api/findings/{fid}", json={"status": "approved"})
    assert stale.status_code == 409
    assert finding_repo.get(conn, fid)["status"] == "edited"


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


def test_pr_runs_404_for_missing_pr(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/prs/99999/runs").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/99999/findings",
        "/api/runs/99999/vendor-results",
        "/api/runs/99999/post-preview",
    ],
)
def test_run_subresources_404_for_missing_run(tmp_path, path):
    client, _ = _client(tmp_path)
    assert client.get(path).status_code == 404


def test_run_context_404_for_missing_run(tmp_path):
    client, _ = _client(tmp_path)
    r = client.get("/api/runs/99999/context")
    assert r.status_code == 404


def test_trigger_review_404_for_missing_pr(tmp_path):
    # 수동 트리거가 1급 경로 — stale/bogus pid는 500이 아니라 404로 정직하게 거부
    client, _ = _client(tmp_path)
    r = client.post("/api/prs/99999/review")
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


def _seed_partial_fail_run(conn, *, number=70):
    """claude done + codex failed 상태의 done run(부분 실패) 시드."""
    from server.repos import pr_repo, repo_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=number,
        title="t",
        author="a",
        head_sha="S",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="S", trigger="manual", effort="medium"
    )
    review_repo.add_vendor_result(conn, run_id=run_id, vendor="claude", status="done")
    review_repo.add_vendor_result(
        conn, run_id=run_id, vendor="codex", status="failed", error="boom"
    )
    review_repo.finish_run(conn, run_id, "done")
    return pid, run_id


def test_retry_vendors_enqueues_retry_job(tmp_path):
    client, conn = _client(tmp_path)
    _, run_id = _seed_partial_fail_run(conn)
    r = client.post(f"/api/runs/{run_id}/retry-vendors")
    assert r.status_code == 202
    job = conn.execute(
        "SELECT trigger, status, retry_run_id FROM review_job WHERE id=?",
        (r.json()["job_id"],),
    ).fetchone()
    assert job["trigger"] == "retry" and job["status"] == "queued"
    assert job["retry_run_id"] == run_id  # 검증된 바로 그 run이 대상으로 전파됨


def test_retry_vendors_rejects_past_run_even_when_head_is_unchanged(tmp_path):
    from server.repos import review_repo

    client, conn = _client(tmp_path)
    pid, old_run = _seed_partial_fail_run(conn, number=78)
    newer = review_repo.create_run(
        conn, pr_id=pid, head_sha="S", trigger="manual", effort="medium"
    )
    review_repo.finish_run(conn, newer, "done")

    response = client.post(f"/api/runs/{old_run}/retry-vendors")

    assert response.status_code == 409
    assert "과거 리뷰 run" in response.json()["detail"]
    assert conn.execute("SELECT COUNT(*) FROM review_job").fetchone()[0] == 0


def test_retry_vendors_rejects_different_run_when_same_head_job_is_active(tmp_path):
    from server.repos import review_repo

    client, conn = _client(tmp_path)
    pid, first_run = _seed_partial_fail_run(conn, number=76)
    first = client.post(f"/api/runs/{first_run}/retry-vendors")
    assert first.status_code == 202

    second_run = review_repo.create_run(
        conn, pr_id=pid, head_sha="S", trigger="manual", effort="medium"
    )
    review_repo.add_vendor_result(
        conn, run_id=second_run, vendor="claude", status="done"
    )
    review_repo.add_vendor_result(
        conn, run_id=second_run, vendor="codex", status="failed", error="boom"
    )
    review_repo.finish_run(conn, second_run, "done")

    second = client.post(f"/api/runs/{second_run}/retry-vendors")

    assert second.status_code == 409
    job = conn.execute(
        "SELECT retry_run_id FROM review_job WHERE id=?", (first.json()["job_id"],)
    ).fetchone()
    assert job["retry_run_id"] == first_run


def test_retry_vendors_409_when_failed_vendor_disabled(tmp_path):
    client, conn = _client(tmp_path)
    pid, run_id = _seed_partial_fail_run(conn, number=73)
    # 실패한 codex를 비활성화 → 재시도해도 worker가 걸러 무동작이므로 엔드포인트가 거절
    repo_id = conn.execute(
        "SELECT repo_id FROM pull_request WHERE id=?", (pid,)
    ).fetchone()["repo_id"]
    conn.execute("UPDATE repo SET vendor_codex_on=0 WHERE id=?", (repo_id,))
    conn.commit()
    r = client.post(f"/api/runs/{run_id}/retry-vendors")
    assert r.status_code == 409 and "실패 벤더" in r.json()["detail"]


@pytest.mark.parametrize("blocked", ["closed", "disabled"])
def test_retry_vendors_rejects_unreviewable_repo_without_job(tmp_path, blocked):
    from server.repos import pr_repo, repo_repo

    client, conn = _client(tmp_path)
    pid, run_id = _seed_partial_fail_run(conn, number=74)
    repo_id = pr_repo.get(conn, pid)["repo_id"]
    if blocked == "closed":
        pr_repo.mark_closed(conn, repo_id, {74})
    else:
        repo_repo.update(conn, repo_id, enabled=0)

    response = client.post(f"/api/runs/{run_id}/retry-vendors")

    assert response.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM review_job").fetchone()[0] == 0


def test_retry_vendors_404_when_run_missing(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/runs/999/retry-vendors").status_code == 404


def test_retry_vendors_409_when_head_advanced(tmp_path):
    client, conn = _client(tmp_path)
    pid, run_id = _seed_partial_fail_run(conn)
    conn.execute("UPDATE pull_request SET head_sha='NEW' WHERE id=?", (pid,))
    conn.commit()
    r = client.post(f"/api/runs/{run_id}/retry-vendors")
    assert r.status_code == 409 and "전체 재리뷰" in r.json()["detail"]


def test_retry_vendors_409_when_no_failed_vendor(tmp_path):
    client, conn = _client(tmp_path)
    from server.repos import pr_repo, repo_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=71,
        title="t",
        author="a",
        head_sha="S",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="S", trigger="manual", effort="medium"
    )
    review_repo.add_vendor_result(conn, run_id=run_id, vendor="claude", status="done")
    review_repo.finish_run(conn, run_id, "done")
    r = client.post(f"/api/runs/{run_id}/retry-vendors")
    assert r.status_code == 409 and "실패 벤더" in r.json()["detail"]


def test_retry_vendors_409_when_run_not_done(tmp_path):
    client, conn = _client(tmp_path)
    from server.repos import pr_repo, repo_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=72,
        title="t",
        author="a",
        head_sha="S",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="S", trigger="manual", effort="medium"
    )
    review_repo.add_vendor_result(
        conn, run_id=run_id, vendor="codex", status="failed", error="boom"
    )
    review_repo.finish_run(conn, run_id, "failed", error="all vendors failed")
    r = client.post(f"/api/runs/{run_id}/retry-vendors")
    assert r.status_code == 409


def test_polled_enqueue_reports_all_system_cancel_revivals(tmp_path):
    from server.api import _enqueue_polled_pr
    from server.repos import job_repo, pr_repo, repo_repo

    _, conn = _client(tmp_path)
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=75,
        title="t",
        author="a",
        head_sha="S",
        base_ref="main",
        url="u",
    )
    jid = job_repo.enqueue(conn, pr_id=pid, head_sha="S", trigger="auto")
    job_repo.mark_canceled(
        conn, jid, error=job_repo.DISABLED_REPO_CANCEL_ERROR
    )

    assert _enqueue_polled_pr(conn, pid) is True
    assert conn.execute(
        "SELECT status FROM review_job WHERE id=?", (jid,)
    ).fetchone()["status"] == "queued"


def test_patch_skip_draft_toggle(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/settings").json()["skip_draft_on"] == 1  # 기본 skip
    assert (
        client.patch("/api/settings", json={"skip_draft_on": 0}).json()["skip_draft_on"]
        == 0
    )
    created = client.post("/api/repos", json={"full_name": "acme/api"}).json()
    assert created["skip_draft_on"] is None  # NULL = 전역 상속
    assert (
        client.patch(f"/api/repos/{created['id']}", json={"skip_draft_on": 1}).json()[
            "skip_draft_on"
        ]
        == 1
    )


def test_repo_review_policy_modes_validate_and_allow_inherit(tmp_path):
    client, _ = _client(tmp_path)
    repo = client.post("/api/repos", json={"full_name": "acme/api"}).json()

    updated = client.patch(
        f"/api/repos/{repo['id']}",
        json={"review_scope_guard_mode": "enforce", "review_dedupe_mode": "observe"},
    )
    assert updated.status_code == 200
    assert updated.json()["review_scope_guard_mode"] == "enforce"
    assert updated.json()["review_dedupe_mode"] == "observe"
    assert updated.json()["requested_review_scope_mode"] == "enforce"
    assert updated.json()["effective_review_scope_mode"] == "observe"
    assert updated.json()["effective_review_scope_reason"] == "benchmark_gate_locked"
    assert updated.json()["review_scope_selection_source"] == "repo_override"
    assert updated.json()["policy_decision"]["scope"] == {
        "requested_mode": "enforce",
        "effective_mode": "observe",
        "reason": "benchmark_gate_locked",
        "selection_source": "repo_override",
    }
    assert client.patch(
        f"/api/repos/{repo['id']}", json={"review_scope_guard_mode": "invalid"}
    ).status_code == 400
    inherited = client.patch(
        f"/api/repos/{repo['id']}", json={"review_scope_guard_mode": ""}
    )
    assert inherited.status_code == 200
    assert inherited.json()["review_scope_guard_mode"] is None


def test_vendor_result_raw_endpoint_is_disabled(tmp_path):
    client, conn = _client(tmp_path)
    raw_file = tmp_path / "vr7.txt"
    raw_file.write_text("벤더 원문 출력입니다", encoding="utf-8")
    rid = conn.execute("INSERT INTO repo (full_name) VALUES ('acme/api')").lastrowid
    pid = conn.execute(
        "INSERT INTO pull_request (repo_id, number, head_sha) VALUES (?, 7, 's1')",
        (rid,),
    ).lastrowid
    run_id = conn.execute(
        "INSERT INTO review_run (pr_id, head_sha) VALUES (?, 's1')", (pid,)
    ).lastrowid
    vr_with = conn.execute(
        "INSERT INTO vendor_result (run_id, vendor, raw_path) VALUES (?, 'claude', ?)",
        (run_id, str(raw_file)),
    ).lastrowid
    conn.commit()

    response = client.get(f"/api/vendor-results/{vr_with}/raw")
    assert response.status_code == 404
    assert "원문" not in response.text or "비활성" in response.text
    assert client.get("/api/vendor-results/99999/raw").status_code == 404


def test_vendor_results_expose_sanitized_execution_meta_without_raw_path(tmp_path):
    from server.review.vendor_telemetry import build_execution_envelope
    from server.repos import review_repo

    client, conn = _client(tmp_path)
    rid = conn.execute("INSERT INTO repo (full_name) VALUES ('acme/api')").lastrowid
    pid = conn.execute(
        "INSERT INTO pull_request (repo_id, number, head_sha) VALUES (?, 8, 's1')",
        (rid,),
    ).lastrowid
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s1", trigger="manual", effort="high"
    )
    vr_id = review_repo.add_vendor_result(
        conn, run_id=run_id, vendor="codex", status="running", raw_path="/private/raw"
    )
    chunk = {
        "index": 0, "status": "done", "safe_error_code": None,
        "duration_ms": 12, "input_tokens": 10, "cached_input_tokens": 3,
        "output_tokens": 4, "reasoning_output_tokens": 1, "total_tokens": 14,
        "tool_calls": 2, "event_count": 5, "stream_truncated": False,
        "telemetry_status": "ok", "cli_name": "codex",
        "cli_version": "codex-cli 0.144.5",
        "event_schema": "codex-jsonl-v0.144.5",
        "chunk_hash": "a" * 64, "context_hash": "b" * 64,
        "chunker_version": "char-v1", "prompt_nonce": "1234abcd",
        "scope_reassigned": 0,
        "scope_rejected": 0, "duplicate_groups": 0,
    }
    review_repo.finish_vendor_result(
        conn, vr_id, status="done", execution_meta=build_execution_envelope(
            identity={
                "protocol_version": "legacy-v0", "vendor": "codex",
                "model": "gpt", "effort": "high", "prompt_hash": "1" * 64,
                "harness_config_hash": "2" * 64, "adapter_name": "adapter",
                "adapter_version": "v1", "adapter_config_hash": "3" * 64,
                "cli_version": "codex-cli 0.144.5",
                "event_schema_version": "codex-jsonl-v0.144.5",
                "diff_hash": "4" * 64, "context_hash": "5" * 64,
                "chunker_version": "char-v1", "scope_policy_mode": "observe",
                "dedupe_policy_mode": "observe",
                "policy_decision_hash": "6" * 64,
                "policy_config_hash": "7" * 64,
            },
            attempt=1, phase="review", chunks=[chunk],
        )
    )

    result = client.get(f"/api/runs/{run_id}/vendor-results").json()[0]

    assert "raw_path" not in result
    assert result["execution_meta"]["attempts"][0]["chunks"][0]["total_tokens"] == 14
    assert "/private" not in json.dumps(result)


def test_cancel_queued_review(tmp_path):
    from server.repos import job_repo, pr_repo, repo_repo

    client, conn = _client(tmp_path)
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="s1",
        base_ref="main",
        url="u",
    )
    assert client.post(f"/api/prs/{pid}/cancel-review").status_code == 404  # 잡 없음

    job_id = job_repo.enqueue(conn, pr_id=pid, head_sha="s1", trigger="auto")
    r = client.post(f"/api/prs/{pid}/cancel-review")
    assert r.status_code == 200
    j = conn.execute("SELECT * FROM review_job WHERE id=?", (job_id,)).fetchone()
    assert j["status"] == "canceled" and "취소" in j["error"]

    # running 잡은 취소 불가(벤더 subprocess를 중단할 수 없음) → 409
    conn.execute("UPDATE review_job SET status='running' WHERE id=?", (job_id,))
    conn.commit()
    assert client.post(f"/api/prs/{pid}/cancel-review").status_code == 409


def test_pr_run_history(tmp_path):
    from server.repos import pr_repo, repo_repo, review_repo
    from server.review.finding_policy import resolve_policy_snapshot

    client, conn = _client(tmp_path)
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="s2",
        base_ref="main",
        url="u",
    )
    r1 = review_repo.create_run(
        conn, pr_id=pid, head_sha="s1", trigger="auto", effort="medium"
    )
    review_repo.finish_run(conn, r1, "done")
    conn.execute(
        "INSERT INTO finding (run_id, vendor, claim) VALUES (?, 'claude', 'c')", (r1,)
    )
    r2 = review_repo.create_run(
        conn, pr_id=pid, head_sha="s2", trigger="manual", effort="medium",
        policy_snapshot=resolve_policy_snapshot(repo_repo.get(conn, rid)),
    )
    review_repo.finish_run(conn, r2, "failed", error="boom")
    conn.commit()

    rows = client.get(f"/api/prs/{pid}/runs").json()
    assert [r["id"] for r in rows] == [r2, r1]  # 최신 먼저
    by_id = {r["id"]: r for r in rows}
    assert by_id[r1]["finding_count"] == 1 and by_id[r1]["status"] == "done"
    assert by_id[r1]["policy_snapshot"]["snapshot_status"] == "unknown"
    assert by_id[r2]["finding_count"] == 0 and by_id[r2]["head_sha"] == "s2"
    assert by_id[r2]["policy_snapshot"]["snapshot_status"] == "known"
    assert by_id[r2]["policy_snapshot"]["scope"]["effective_mode"] == "observe"
    assert client.get("/api/prs/999/runs").status_code == 404


def test_cancel_review_cancels_all_queued_jobs(tmp_path):
    from server.repos import job_repo, pr_repo, repo_repo

    client, conn = _client(tmp_path)
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="s2",
        base_ref="main",
        url="u",
    )
    # 구 sha 잡(재시도 대기)과 새 sha 잡이 공존 — 취소는 둘 다 잡아야 한다
    job_repo.enqueue(conn, pr_id=pid, head_sha="s1", trigger="auto")
    job_repo.enqueue(conn, pr_id=pid, head_sha="s2", trigger="auto")
    r = client.post(f"/api/prs/{pid}/cancel-review")
    assert r.status_code == 200 and r.json()["canceled"] == 2
    left = conn.execute(
        "SELECT COUNT(*) c FROM review_job WHERE status='queued'"
    ).fetchone()["c"]
    assert left == 0


def test_post_run_rejects_stale_head(tmp_path):
    from server.repos import finding_repo, pr_repo, repo_repo, review_repo

    client, conn = _client(tmp_path)
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="s2",
        base_ref="main",
        url="u",
    )
    run = review_repo.create_run(
        conn, pr_id=pid, head_sha="s1", trigger="manual", effort="medium"
    )
    fid = finding_repo.add(
        conn,
        run_id=run,
        vendor="claude",
        file="a.py",
        line=1,
        severity="high",
        category="bug",
        claim="c",
        rationale="r",
        confidence=0.9,
    )
    finding_repo.set_status(conn, fid, "approved")
    r = client.post(f"/api/runs/{run}/post")
    assert (
        r.status_code == 409
    )  # head가 전진한 구 run은 게시 금지(잘못된 앵커·덮어쓰기 방지)
