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


def test_patch_settings_context_toggles(tmp_path):
    client, _ = _client(tmp_path)
    r = client.patch(
        "/api/settings", json={"context_static_on": 1, "context_jira_on": 1}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["context_static_on"] == 1 and body["context_jira_on"] == 1


def test_patch_settings_rejects_invalid_threshold(tmp_path):
    # 임의 문자열이 저장되면 decide()가 KeyError로 죽어 이후 모든 리뷰가 실패한다.
    client, _ = _client(tmp_path)
    r = client.patch("/api/settings", json={"prescreen_gate_threshold": "extreme"})
    assert r.status_code == 400
    assert client.get("/api/settings").json()["prescreen_gate_threshold"] != "extreme"
    ok = client.patch("/api/settings", json={"prescreen_gate_threshold": "complex"})
    assert ok.status_code == 200
    assert ok.json()["prescreen_gate_threshold"] == "complex"


def test_add_repo_duplicate_returns_409(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/repos", json={"full_name": "acme/api"}).status_code == 201
    assert client.post("/api/repos", json={"full_name": "acme/api"}).status_code == 409


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
    assert body["graphify_path"] == "docs/PROJECT.md"


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
