from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient

from server.api import app, get_conn
from server.db import connect, init_schema
from server.repos import operations_repo, review_repo
from server.review.vendor_telemetry import build_execution_envelope


def _client(tmp_path):
    conn = connect(tmp_path / "operations.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    return TestClient(app), conn


def _seed_repo(conn, name: str):
    repo_id = conn.execute("INSERT INTO repo(full_name) VALUES (?)", (name,)).lastrowid
    pr_id = conn.execute(
        """INSERT INTO pull_request(repo_id,number,title,head_sha,state)
           VALUES (?,1,'PR title','head','open')""",
        (repo_id,),
    ).lastrowid
    return repo_id, pr_id


def _seed_run(conn, *, repo_id: int, pr_id: int, status: str, age_hours: int,
              duration_ms: int = 100, error: str | None = None, vendor="codex",
              vendor_status="done"):
    finished = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    started = finished - timedelta(milliseconds=duration_ms)
    run_id = conn.execute(
        """INSERT INTO review_run(
               pr_id,repo_id,head_sha,status,trigger,started_at,finished_at,error
           ) VALUES (?,?,? ,?,'manual',?,?,?)""",
        (
            pr_id, repo_id, "head", status,
            operations_repo._timestamp(started), operations_repo._timestamp(finished),
            error,
        ),
    ).lastrowid
    conn.execute(
        """INSERT INTO vendor_result(run_id,vendor,status,duration_ms,error)
           VALUES (?,?,?,?,?)""",
        (run_id, vendor, vendor_status, duration_ms, error),
    )
    conn.commit()
    return run_id


def test_dashboard_defaults_to_all_repos_and_reports_core_metrics(tmp_path):
    client, conn = _client(tmp_path)
    repo_a, pr_a = _seed_repo(conn, "acme/a")
    repo_b, pr_b = _seed_repo(conn, "acme/b")
    _seed_run(conn, repo_id=repo_a, pr_id=pr_a, status="done", age_hours=1,
              duration_ms=100)
    failed = _seed_run(
        conn, repo_id=repo_b, pr_id=pr_b, status="failed", age_hours=2,
        duration_ms=300, error="/private/secret token authentication failed",
        vendor="claude", vendor_status="timeout",
    )

    response = client.get("/api/operations/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert body["filters"] == {"repo_id": None, "range": "24h"}
    assert body["summary"]["sampled_runs"] == 2
    assert body["summary"]["statuses"] == {"done": 1, "failed": 1}
    assert body["summary"]["success"] == {
        "numerator": 1, "denominator": 2, "rate": 0.5,
    }
    assert body["summary"]["latency_ms"] == {
        "denominator": 2, "p50": 100, "p95": 300,
    }
    failure = body["summary"]["recent_failures"][0]
    assert failure["run_id"] == failed
    assert failure["failure_code"] == "authentication"
    assert failure["repo"]["full_name"] == "acme/b"
    rendered = json.dumps(body)
    for private in ("/private", "secret token", "raw_path", "prompt", "claim", "rationale"):
        assert private not in rendered
    app.dependency_overrides.clear()
    conn.close()


def test_dashboard_repo_and_range_filters_are_exact(tmp_path):
    client, conn = _client(tmp_path)
    repo_a, pr_a = _seed_repo(conn, "acme/a")
    repo_b, pr_b = _seed_repo(conn, "acme/b")
    _seed_run(conn, repo_id=repo_a, pr_id=pr_a, status="done", age_hours=25)
    _seed_run(conn, repo_id=repo_b, pr_id=pr_b, status="done", age_hours=1)

    last_day = client.get(
        "/api/operations/dashboard", params={"repo_id": repo_a, "range": "24h"}
    ).json()
    week = client.get(
        "/api/operations/dashboard", params={"repo_id": repo_a, "range": "7d"}
    ).json()
    assert last_day["summary"]["sampled_runs"] == 0
    assert week["summary"]["sampled_runs"] == 1
    assert week["filters"] == {"repo_id": repo_a, "range": "7d"}
    app.dependency_overrides.clear()
    conn.close()


def test_dashboard_lists_active_jobs_and_sanitizes_errors(tmp_path):
    client, conn = _client(tmp_path)
    repo_id, pr_id = _seed_repo(conn, "acme/api")
    conn.execute(
        """INSERT INTO review_job(
               pr_id,head_sha,trigger,status,attempts,max_attempts,error,created_at
           ) VALUES (?,'head','auto','queued',1,3,'/tmp/private timeout',datetime('now'))""",
        (pr_id,),
    )
    conn.commit()
    body = client.get(
        "/api/operations/dashboard", params={"repo_id": repo_id}
    ).json()
    assert body["active_jobs"]["total"] == 1
    job = body["active_jobs"]["jobs"][0]
    assert job["status"] == "queued"
    assert job["failure_code"] == "timeout"
    assert "/tmp" not in json.dumps(job)
    app.dependency_overrides.clear()
    conn.close()


def test_dashboard_rejects_invalid_range_and_is_management_authenticated(
    tmp_path, monkeypatch
):
    client, conn = _client(tmp_path)
    assert client.get(
        "/api/operations/dashboard", params={"range": "14d"}
    ).status_code == 400
    monkeypatch.setattr("server.config.ADMIN_TOKEN", "a" * 32)
    assert client.get("/api/operations/dashboard").status_code == 401
    assert client.get(
        "/api/operations/dashboard",
        headers={"Authorization": "Bearer " + "a" * 32},
    ).status_code == 200
    app.dependency_overrides.clear()
    conn.close()


def test_dashboard_scan_is_hard_capped_and_disclosed(tmp_path, monkeypatch):
    client, conn = _client(tmp_path)
    repo_id, pr_id = _seed_repo(conn, "acme/api")
    for age in (1, 2, 3):
        _seed_run(conn, repo_id=repo_id, pr_id=pr_id, status="done", age_hours=age)
    monkeypatch.setattr(operations_repo, "MAX_RUNS", 2)
    body = client.get("/api/operations/dashboard").json()["summary"]
    assert body["sampled_runs"] == body["scan_limit"] == 2
    assert body["truncated"] is True
    app.dependency_overrides.clear()
    conn.close()


def test_dashboard_failure_cap_has_its_own_denominator(tmp_path, monkeypatch):
    client, conn = _client(tmp_path)
    repo_id, pr_id = _seed_repo(conn, "acme/api")
    for age in (1, 2, 3):
        _seed_run(
            conn, repo_id=repo_id, pr_id=pr_id, status="failed",
            age_hours=age, error="timeout", vendor_status="timeout",
        )
    monkeypatch.setattr(operations_repo, "MAX_FAILURES", 2)
    summary = client.get("/api/operations/dashboard").json()["summary"]
    assert summary["recent_failure_summary"] == {
        "total": 3, "listed": 2, "truncated": True,
    }
    assert len(summary["recent_failures"]) == 2
    assert summary["truncated"] is False
    app.dependency_overrides.clear()
    conn.close()


def _execution_meta():
    chunk = {
        "index": 0, "status": "timeout", "safe_error_code": "timeout",
        "duration_ms": 12, "input_tokens": 10, "cached_input_tokens": 0,
        "output_tokens": 4, "reasoning_output_tokens": 1, "total_tokens": 14,
        "tool_calls": 2, "event_count": 5, "stream_truncated": False,
        "telemetry_status": "partial", "cli_name": "codex",
        "cli_version": "codex-cli", "event_schema": "codex-jsonl",
        "chunk_hash": "a" * 64, "context_hash": "b" * 64,
        "chunker_version": "char-v1", "prompt_nonce": "1234abcd",
        "scope_reassigned": 1, "scope_rejected": 0, "duplicate_groups": 0,
    }
    return build_execution_envelope(
        identity={
            "protocol_version": "legacy-v0", "vendor": "codex", "model": "gpt",
            "effort": "high", "prompt_hash": "1" * 64,
            "harness_config_hash": "2" * 64, "adapter_name": "adapter",
            "adapter_version": "v1", "adapter_config_hash": "3" * 64,
            "cli_version": "codex-cli", "event_schema_version": "codex-jsonl",
            "diff_hash": "4" * 64, "context_hash": "5" * 64,
            "chunker_version": "char-v1", "scope_policy_mode": "observe",
            "dedupe_policy_mode": "observe", "policy_decision_hash": "6" * 64,
            "policy_config_hash": "7" * 64,
        },
        attempt=1, phase="review", chunks=[chunk],
    )


def test_retry_diagnostics_reject_conflicting_active_same_head_job(tmp_path):
    client, conn = _client(tmp_path)
    _, pr_id = _seed_repo(conn, "acme/api")
    run_id = review_repo.create_run(
        conn, pr_id=pr_id, head_sha="head", trigger="manual", effort="high"
    )
    conn.execute("UPDATE review_run SET status='done' WHERE id=?", (run_id,))
    conn.execute(
        """INSERT INTO vendor_result(run_id,vendor,status)
           VALUES (?,'codex','timeout')""",
        (run_id,),
    )
    conn.execute(
        """INSERT INTO review_job(pr_id,head_sha,trigger,status)
           VALUES (?,'head','manual','running')""",
        (pr_id,),
    )
    conn.commit()
    retry = client.get(f"/api/runs/{run_id}/diagnostics").json()["retry"]
    assert retry["mode"] == "retry_unavailable"
    assert "active_job_conflict" in retry["reasons"]
    app.dependency_overrides.clear()
    conn.close()


def test_run_diagnostics_are_actionable_and_content_free(tmp_path):
    client, conn = _client(tmp_path)
    repo_id, pr_id = _seed_repo(conn, "acme/api")
    run_id = review_repo.create_run(
        conn, pr_id=pr_id, head_sha="head", trigger="manual", effort="high"
    )
    conn.execute(
        """UPDATE review_run SET status='done',finished_at=datetime('now'),
                   base_sha='base' WHERE id=?""",
        (run_id,),
    )
    vr_id = review_repo.add_vendor_result(
        conn, run_id=run_id, vendor="codex", status="running", raw_path="/private/raw"
    )
    review_repo.finish_vendor_result(
        conn, vr_id, status="timeout", error="/private/raw timed out",
        execution_meta=_execution_meta(),
    )
    conn.execute(
        """INSERT INTO finding(
               run_id,vendor,file,line,severity,claim,rationale,status,
               scope_status,posting_eligible
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (run_id, "codex", "src/a.py", 1, "high", "secret claim",
         "secret rationale", "approved", "reassigned", 0),
    )
    conn.commit()

    response = client.get(f"/api/runs/{run_id}/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert body["run"]["review_scope"] == "incremental"
    assert body["processing"] == {
        "attempts": 1, "chunks": 1, "chunk_statuses": {"timeout": 1},
        "safe_error_codes": {"timeout": 1},
        "telemetry": {"denominator": 1, "ok": 0, "partial": 1, "unavailable": 0},
        "tokens": 14, "tool_calls": 2,
    }
    assert body["findings"]["files"] == 1
    assert body["findings"]["posting"] == {"eligible": 0, "suppressed": 1}
    assert body["retry"] == {
        "mode": "failed_vendors", "failed_vendors": ["codex"], "reasons": [],
    }
    rendered = json.dumps(body)
    for private in ("/private", "secret claim", "secret rationale", "raw_path", "execution_meta"):
        assert private not in rendered
    app.dependency_overrides.clear()
    conn.close()
