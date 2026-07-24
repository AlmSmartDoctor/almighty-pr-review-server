import asyncio
import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import config
from server.api import app, get_conn
from server.db import connect, init_schema
from server.repos import job_repo, pr_repo, repo_repo, review_repo
from server.worker import run_one_job
from tests.test_e2e_smoke import e2e_state_message

_spec = importlib.util.spec_from_file_location("sandbox_e2e_diagnostics", Path(__file__).parents[1] / "scripts" / "sandbox-e2e.py")
sandbox = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(sandbox)


def test_complete_paging_refuses_truncation_or_cap_and_snapshot_requires_all_surfaces():
    with pytest.raises(sandbox.PreflightError, match="truncated"):
        sandbox.complete_page_snapshot(lambda page, size: {"items": [], "truncated": True})
    with pytest.raises(sandbox.PreflightError, match="cap"):
        sandbox.complete_page_snapshot(lambda page, size: {"items": [], "complete": False}, max_pages=2)
    pages = {1: {"items": [{"id": 1}], "complete": False}, 2: {"items": [{"id": 2}], "complete": True}}
    assert sandbox.complete_page_snapshot(lambda page, size: pages[page]) == [{"id": 1}, {"id": 2}]
    with pytest.raises(sandbox.PreflightError, match="all remote"):
        sandbox.snapshot_digest({"reviews": [], "head": []})


def test_preflight_cli_failure_is_sanitized(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "private-manifest-name.json"
    monkeypatch.setattr(
        sys, "argv",
        [
            "sandbox-e2e.py", "--manifest", str(missing),
            "--allowlist", str(tmp_path / "private-allowlist-name.json"),
            "--credential-attestation", str(tmp_path / "private-attestation.json"),
            "--credential-attestation-sha256", "0" * 64,
        ],
    )
    assert sandbox.main() == 1
    captured = capsys.readouterr()
    assert "preflight_failed" in captured.err
    assert "private-manifest-name" not in captured.out + captured.err


def test_isolated_cleanup_failure_does_not_mask_active_error(monkeypatch):
    active = RuntimeError("active")
    real_rmtree = sandbox.shutil.rmtree
    directory = None
    monkeypatch.setattr(
        sandbox.shutil, "rmtree",
        lambda path: (_ for _ in ()).throw(OSError("cleanup")),
    )
    try:
        with pytest.raises(RuntimeError, match="active") as caught:
            with sandbox.isolated_gh_environment(
                "credential",
                expected_fingerprint=sandbox.credential_fingerprint("credential"),
                base_env={},
            ) as env:
                directory = Path(env["GH_CONFIG_DIR"])
                raise active
        assert "isolated GH_CONFIG_DIR cleanup failed" in getattr(
            caught.value, "__notes__", []
        )
    finally:
        if directory is not None:
            real_rmtree(directory)


def test_snapshot_evidence_detects_remote_mutation_without_raw_transcript():
    preflight = {"target": "acme/api#7", "allowlist_hash": "a" * 64, "phase": "review", "head_sha": "h", "vendor": "fake", "model": "m", "live": "not_run"}
    before = {"reviews": [], "inline_comments": [], "conversation_comments": [], "head": [{"sha": "h"}]}
    after = {**before, "reviews": [{"id": "unexpected"}]}
    evidence = sandbox.sanitized_evidence(preflight, before=before, after=after)
    assert evidence["mutation_snapshot_match"] is False
    assert "credential" not in json.dumps(evidence).lower()


def test_worker_injection_requires_done_terminal_state_and_fake_call_counts(tmp_path, monkeypatch):
    conn = connect(tmp_path / "e2e.db")
    init_schema(conn)
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(conn, repo_id=rid, number=7, title="t", author="a", head_sha="h", base_ref="main", url="u")
    job_repo.enqueue(conn, pr_id=pid, head_sha="h", trigger="manual")
    calls = {"deps": 0, "runner": 0}
    async def fake_review(*args, **kwargs):
        calls["runner"] += 1
        return review_repo.create_run(conn, pr_id=pid, head_sha="h", trigger="manual", effort="medium")
    monkeypatch.setattr("server.worker.review_pr", fake_review)
    assert asyncio.run(run_one_job(conn, worker_id="offline", deps_builder=lambda *a, **kw: calls.__setitem__("deps", calls["deps"] + 1) or object()))
    row = conn.execute("SELECT status FROM review_job").fetchone()
    assert row["status"] == "done" and calls == {"deps": 1, "runner": 1}
    # A failed terminal row is diagnostics only, never a successful rehearsal state.
    conn.execute("UPDATE review_job SET status='failed'")
    conn.commit()
    assert conn.execute("SELECT status FROM review_job").fetchone()["status"] != "done"
    assert "failed" in e2e_state_message(conn)


def test_signed_webhook_replay_is_in_process_and_duplicate_stays_one_unconsumed_job(tmp_path, monkeypatch):
    calls = {"vendor": 0, "github_write": 0, "worker": 0}

    async def forbidden_vendor(*args, **kwargs):
        calls["vendor"] += 1
        raise AssertionError("webhook replay must not invoke a vendor")

    async def forbidden_worker(*args, **kwargs):
        calls["worker"] += 1
        raise AssertionError("webhook replay must not consume the job")

    def forbidden_write(*args, **kwargs):
        calls["github_write"] += 1
        raise AssertionError("webhook replay must not write to GitHub")

    monkeypatch.setattr("server.pipeline.review_pr", forbidden_vendor)
    monkeypatch.setattr("server.worker.run_one_job", forbidden_worker)
    monkeypatch.setattr("server.github.gh.GhClient.create_review", forbidden_write)
    secret = "offline-secret"
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", secret)
    conn = connect(tmp_path / "webhook.db")
    init_schema(conn)
    repo_repo.add(conn, full_name="acme/api")
    app.dependency_overrides[get_conn] = lambda: conn
    body = json.dumps({"action": "synchronize", "repository": {"full_name": "acme/api"}, "pull_request": {"number": 7, "title": "t", "user": {"login": "a"}, "html_url": "u", "state": "open", "head": {"sha": "h", "ref": "f"}, "base": {"ref": "main", "sha": "b"}}}).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    client = TestClient(app)  # no context manager: no lifespan/background consumer
    headers = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": signature}
    assert client.post("/api/webhooks/github", content=body, headers=headers).json()["status"] == "enqueued"
    assert client.post("/api/webhooks/github", content=body, headers=headers).json()["status"] in {"skipped", "enqueued"}
    jobs = conn.execute(
        "SELECT status, attempts, run_id FROM review_job"
    ).fetchall()
    assert len(jobs) == 1
    assert dict(jobs[0]) == {"status": "queued", "attempts": 0, "run_id": None}
    assert conn.execute("SELECT COUNT(*) FROM review_run").fetchone()[0] == 0
    assert calls == {"vendor": 0, "github_write": 0, "worker": 0}
    # Same signed payload cannot be consumed by a worker or invoke vendor/GitHub writes.
    monkeypatch.setattr(config, "WEBHOOK_MAX_BODY_BYTES", len(body) - 1)
    assert client.post(
        "/api/webhooks/github", content=body, headers=headers
    ).status_code == 413
    assert conn.execute("SELECT COUNT(*) FROM review_job").fetchone()[0] == 1
    assert calls == {"vendor": 0, "github_write": 0, "worker": 0}
    app.dependency_overrides.clear()
