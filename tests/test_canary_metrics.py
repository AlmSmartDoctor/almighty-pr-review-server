from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient

from server.api import app, get_conn
from server.db import connect, init_schema
from server.repos import canary_repo


def _client(tmp_path):
    conn = connect(tmp_path / "canary.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    return TestClient(app), conn


def _seed(conn, *, started_at, cohort="observe", run_status="done", telemetry=True):
    if started_at is not None:
        parsed = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        started_at = canary_repo._sqlite_timestamp(parsed)
    row = conn.execute("SELECT id FROM repo WHERE full_name='acme/api'").fetchone()
    repo = row["id"] if row else conn.execute("INSERT INTO repo(full_name) VALUES ('acme/api')").lastrowid
    number = conn.execute("SELECT COUNT(*) FROM pull_request WHERE repo_id=?", (repo,)).fetchone()[0] + 1
    pr = conn.execute("INSERT INTO pull_request(repo_id,number,head_sha) VALUES (?,?,?)", (repo, number, f"head-{number}")).lastrowid
    run = conn.execute(
        """INSERT INTO review_run(pr_id,head_sha,status,started_at,finished_at,
             scope_effective_mode,dedupe_effective_mode,policy_cohort_key)
           VALUES (?,?,?,?,?,?,?,?)""",
        (pr, f"head-{number}", run_status, started_at, started_at, "observe", "observe", cohort),
    ).lastrowid
    meta = json.dumps({"attempts": [{"attempt": 1, "phase": "review", "chunks": [{
        "status": "done", "telemetry_status": "ok", "total_tokens": 11,
        "tool_calls": 2, "duration_ms": 5
    }]}]}) if telemetry else None
    conn.execute("INSERT INTO vendor_result(run_id,vendor,status,duration_ms,execution_meta) VALUES (?,?,?,?,?)", (run, "codex", "done", 5, meta))
    conn.execute("INSERT INTO finding(run_id,vendor,status,scope_status,posting_eligible,duplicate_group_id,duplicate_suggested) VALUES (?,?,?,?,?,?,?)", (run, "codex", "approved", "owned", 1, 1, 1))
    conn.commit()
    return repo, run


def test_summary_and_runs_share_filters_and_do_not_expose_sensitive_fields(tmp_path):
    client, conn = _client(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    repo, first = _seed(conn, started_at=now)
    _seed(conn, started_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    summary = client.get("/api/operations/review-policy/summary", params={"repo_id": repo, "days": 7})
    runs = client.get("/api/operations/review-policy/runs", params={"repo_id": repo, "days": 7})
    assert summary.status_code == runs.status_code == 200
    assert summary.json()["current"]["runs"] == runs.json()["denominator"] == 2
    assert summary.json()["current"]["vendor_final"]["denominator"] == 2
    assert summary.json()["current"]["vendor_attempts"]["denominator"] == 2
    rendered = json.dumps(runs.json())
    for forbidden in ("raw_path", "claim", "rationale", "context", "prompt", "command", "event"):
        assert forbidden not in rendered
    app.dependency_overrides.clear()
    conn.close()


def test_vendor_filter_limits_hydrated_vendor_denominators(tmp_path):
    client, conn = _client(tmp_path)
    repo, run_id = _seed(
        conn, started_at=datetime.now(timezone.utc).isoformat()
    )
    conn.execute(
        """INSERT INTO vendor_result(run_id,vendor,status,duration_ms)
           VALUES (?,?,?,?)""",
        (run_id, "claude", "failed", 7),
    )
    conn.execute(
        """INSERT INTO finding(
               run_id,vendor,status,scope_status,posting_eligible
           ) VALUES (?,?,?,?,?)""",
        (run_id, "claude", "approved", "would_reject", 0),
    )
    conn.commit()
    response = client.get(
        "/api/operations/review-policy/summary",
        params={"repo_id": repo, "days": 7, "vendor": "codex"},
    )
    assert response.status_code == 200
    body = response.json()["current"]
    assert body["vendor_final"]["denominator"] == 1
    assert body["scope"]["owned"] == 1
    app.dependency_overrides.clear()
    conn.close()


def test_legacy_null_timestamp_is_not_duplicated_into_baseline(tmp_path):
    client, conn = _client(tmp_path)
    repo, _ = _seed(conn, started_at=None, cohort=None, telemetry=False)
    response = client.get(
        "/api/operations/review-policy/summary",
        params={"repo_id": repo, "days": 7, "baseline_days": 7},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["current"]["runs"] == 1
    assert body["baseline"]["metrics"]["runs"] == 0
    app.dependency_overrides.clear()
    conn.close()


def test_cursor_is_exclusive_bound_to_filter_and_handles_null_bucket(tmp_path):
    client, conn = _client(tmp_path)
    repo, newest = _seed(conn, started_at=datetime.now(timezone.utc).isoformat())
    _seed(conn, started_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    _seed(conn, started_at=None, cohort=None, telemetry=False)
    first = client.get("/api/operations/review-policy/runs", params={"repo_id": repo, "days": 7, "limit": 2})
    assert first.status_code == 200
    body = first.json()
    assert len(body["runs"]) == 2 and body["next_cursor"]
    second = client.get("/api/operations/review-policy/runs", params={"repo_id": repo, "days": 7, "limit": 2, "cursor": body["next_cursor"]})
    assert second.status_code == 200
    assert [r["id"] for r in body["runs"] + second.json()["runs"]] == [newest, newest + 1, newest + 2]
    assert second.json()["runs"][0]["started_at"] is None
    mismatch = client.get("/api/operations/review-policy/runs", params={"repo_id": repo, "days": 6, "cursor": body["next_cursor"]})
    assert mismatch.status_code == 400
    assert client.get("/api/operations/review-policy/runs", params={"repo_id": repo, "cursor": "bad"}).status_code == 400
    app.dependency_overrides.clear()
    conn.close()


def test_control_status_honors_repo_canary_overrides_and_restart_contract(
    tmp_path, monkeypatch
):
    client, conn = _client(tmp_path)
    repo, _ = _seed(conn, started_at=datetime.now(timezone.utc).isoformat())
    conn.execute(
        """UPDATE repo SET review_scope_guard_mode='enforce',
                           review_dedupe_mode='observe' WHERE id=?""",
        (repo,),
    )
    conn.commit()
    monkeypatch.setattr("server.config.REVIEW_SCOPE_GUARD_MODE", "observe")
    monkeypatch.setattr("server.config.REVIEW_SCOPE_ENFORCE_REPOS", frozenset())
    monkeypatch.setattr("server.config.REVIEW_SCOPE_KILL_SWITCH", False)
    body = client.get(
        "/api/operations/review-policy/summary", params={"repo_id": repo}
    ).json()["control"]
    assert body["scope"] == {
        "configured_mode": "enforce",
        "selection_source": "repo_override",
        "canary_member": True,
        "kill_switch": False,
    }
    assert body["dedupe"]["selection_source"] == "repo_override"
    assert body["restart_required"] is True
    app.dependency_overrides.clear()
    conn.close()


def test_operations_endpoint_requires_existing_management_bearer(tmp_path, monkeypatch):
    client, conn = _client(tmp_path)
    repo, _ = _seed(conn, started_at=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr("server.config.ADMIN_TOKEN", "a" * 32)
    assert client.get("/api/operations/review-policy/summary", params={"repo_id": repo}).status_code == 401
    assert client.get("/api/operations/review-policy/summary", params={"repo_id": repo}, headers={"Authorization": "Bearer " + "a" * 32}).status_code == 200
    app.dependency_overrides.clear()
    conn.close()


def test_windows_are_normalized_and_non_overlapping_at_boundary(db):
    filters = canary_repo.normalize_filters(repo_id=1, days=7, baseline_days=7)
    as_of, current_start, baseline_start = canary_repo.windows(
        filters, as_of="2026-07-23T12:00:00+00:00"
    )
    assert "T" not in as_of and "+" not in as_of
    repo, run_id = _seed(db, started_at=current_start)
    filters = canary_repo.normalize_filters(
        repo_id=repo, days=7, baseline_days=7
    )
    current, _ = canary_repo._select_runs(
        db, filters, start=current_start, end=as_of, cap=10
    )
    baseline, _ = canary_repo._select_runs(
        db, filters, start=baseline_start, end=current_start, cap=10,
        include_null=False, end_inclusive=False,
    )
    assert [row["id"] for row in current] == [run_id]
    assert baseline == []


def test_attempt_duration_and_duplicate_metrics_are_not_double_counted(db):
    now = datetime.now(timezone.utc)
    repo, first = _seed(db, started_at=now.isoformat())
    _, second = _seed(db, started_at=(now - timedelta(minutes=1)).isoformat())
    meta = json.dumps({"attempts": [{"attempt": 1, "phase": "review", "chunks": [
        {"status": "done", "telemetry_status": "ok", "duration_ms": 5},
        {"status": "done", "telemetry_status": "ok", "duration_ms": 6},
    ]}]})
    db.execute("UPDATE vendor_result SET execution_meta=?", (meta,))
    db.commit()
    filters = canary_repo.normalize_filters(repo_id=repo, days=7)
    as_of, start, _ = canary_repo.windows(filters)
    runs, _ = canary_repo._select_runs(db, filters, start=start, end=as_of, cap=10)
    canary_repo.hydrate_runs(db, runs)
    result = canary_repo.metrics(runs)
    assert result["vendor_attempts"]["denominator"] == 2
    assert result["vendor_attempts"]["statuses"] == {"done": 2}
    assert result["aggregates"]["duration_ms"] == 10
    assert result["duplicates"]["groups"] == 2
    assert {row["id"] for row in runs} == {first, second}


def test_operations_query_plan_uses_bounded_indexes(db):
    repo, _ = _seed(db, started_at=datetime.now(timezone.utc).isoformat())
    filters = canary_repo.normalize_filters(repo_id=repo, days=7)
    as_of, start, _ = canary_repo.windows(filters)
    where, params = canary_repo._where(filters, start=start, end=as_of)
    plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT rr.id FROM review_run rr "
        "INDEXED BY idx_review_run_operations_repo "
        f"WHERE {where} ORDER BY (rr.started_at IS NULL), "
        "rr.started_at DESC, rr.id DESC LIMIT 101", params
    ).fetchall()
    detail = " ".join(row[3] for row in plan)
    assert "idx_review_run_operations_repo" in detail
    assert "TEMP B-TREE" not in detail
