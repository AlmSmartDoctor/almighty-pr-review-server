"""Offline recording-transport coverage for the M2 rehearsal posting boundary."""
import hashlib

from fastapi import HTTPException
from fastapi.testclient import TestClient

from server import config
from server import api as api_module
from server.api import (
    RehearsalPostingPolicy,
    _post_run_locked,
    app,
    get_conn,
    get_gh,
    get_slack,
)
from server.db import connect, init_schema
from server.repos import finding_repo, pr_repo, repo_repo, review_repo


class RecordingGh:
    def __init__(self):
        self.mutations = []

    def preflight_user(self): return {"login": "offline"}
    def preflight_repo(self, repo): return {"full_name": repo}
    def preflight_issue(self, repo, number): return {"number": number}
    def get_pr_review_context(self, repo, number): return {"head_sha": "h", "reviews": [], "inline_comments": [], "conversation_comments": []}
    def get_pr_head(self, repo, number): return "h"
    def diff(self, repo, number):
        return "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+x\n"
    def list_pr_reviews(self, repo, number): return []
    def create_review(self, repo, number, commit, body, comments):
        self.mutations.append(("POST", f"/repos/{repo}/pulls/{number}/reviews", body, comments))
        return {"id": 9, "html_url": "https://offline/9"}


def _run(conn, vendors=("claude",)):
    rid = repo_repo.add(conn, full_name="Acme/API")
    pid = pr_repo.upsert(conn, repo_id=rid, number=7, title="t", author="a", head_sha="h", base_ref="main", url="u")
    run_id = review_repo.create_run(conn, pr_id=pid, head_sha="h", trigger="manual", effort="medium")
    conn.execute("UPDATE review_run SET policy_decision_hash='policy-identity' WHERE id=?", (run_id,))
    for index, vendor in enumerate(vendors):
        fid = finding_repo.add(
            conn, run_id=run_id, vendor=vendor, file="a.py", line=1,
            severity="high", category="bug", claim=f"claim-{index}",
            rationale="why", confidence=.9,
        )
        finding_repo.set_status(conn, fid, "approved")
    return run_id


def test_rehearsal_policy_default_denies_create_inline_and_slack(tmp_path):
    conn = connect(tmp_path / "post.db"); init_schema(conn)
    run_id = _run(conn); gh = RecordingGh()
    try:
        _post_run_locked(run_id, conn=conn, gh=gh, slack=object(), rehearsal_policy=RehearsalPostingPolicy(active=True))
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("default-deny rehearsal policy allowed a mutation")
    assert gh.mutations == []
    assert conn.execute(
        "SELECT COUNT(*) FROM github_post_operation"
    ).fetchone()[0] == 0
    finding = conn.execute("SELECT * FROM finding").fetchone()
    assert finding["status"] == "approved"
    assert finding["posting_operation_id"] is None


def test_server_rehearsal_gh_binds_actual_token_and_private_config(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "gh"
    config_dir.mkdir(mode=0o700)
    token = "write-credential"
    monkeypatch.setattr(config, "REHEARSAL_POST_ENABLED", True)
    monkeypatch.setattr(
        config, "REHEARSAL_GH_CREDENTIAL_FINGERPRINT",
        "sha256:" + hashlib.sha256(token.encode()).hexdigest(),
    )
    monkeypatch.setenv("GH_TOKEN", token)
    monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))
    captured = {}

    class StrictClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(api_module, "GhClient", StrictClient)
    client = api_module.get_gh()
    assert isinstance(client, StrictClient)
    assert captured["strict_isolated"] is True
    assert captured["env"]["GH_TOKEN"] == token

    monkeypatch.setattr(
        config, "REHEARSAL_GH_CREDENTIAL_FINGERPRINT", "sha256:wrong"
    )
    try:
        api_module.get_gh()
    except HTTPException as exc:
        assert exc.status_code == 503
    else:
        raise AssertionError("mismatched rehearsal credential was accepted")


def test_public_post_endpoint_applies_server_rehearsal_policy(
    tmp_path, monkeypatch
):
    conn = connect(tmp_path / "post-api.db"); init_schema(conn)
    run_id = _run(conn); gh = RecordingGh()
    monkeypatch.setattr(config, "REHEARSAL_POST_ENABLED", True)
    monkeypatch.setattr(config, "REHEARSAL_POST_TARGET", "acme/api#7")
    monkeypatch.setattr(config, "REHEARSAL_POST_HEAD_SHA", "h")
    for name in (
        "REHEARSAL_ALLOW_CREATE", "REHEARSAL_ALLOW_UPDATE",
        "REHEARSAL_ALLOW_UPDATE_FALLBACK", "REHEARSAL_ALLOW_INLINE",
        "REHEARSAL_ALLOW_SLACK",
    ):
        monkeypatch.setattr(config, name, False)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_gh] = lambda: gh
    app.dependency_overrides[get_slack] = lambda: object()
    try:
        response = TestClient(app).post(f"/api/runs/{run_id}/post")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409
    assert gh.mutations == []
    assert conn.execute(
        "SELECT COUNT(*) FROM github_post_operation"
    ).fetchone()[0] == 0


def test_rehearsal_inline_comment_carries_operation_marker(tmp_path):
    conn = connect(tmp_path / "post-inline.db"); init_schema(conn)
    run_id = _run(conn); gh = RecordingGh()
    policy = RehearsalPostingPolicy(
        active=True, allow_create=True, allow_inline=True
    )
    _post_run_locked(
        run_id, conn=conn, gh=gh, slack=object(), rehearsal_policy=policy
    )
    operation = conn.execute("SELECT * FROM github_post_operation").fetchone()
    assert gh.mutations[0][3]
    assert operation["marker"] in gh.mutations[0][3][0]["body"]


def test_rehearsal_success_replay_is_stable_noop_with_exact_identity(tmp_path):
    conn = connect(tmp_path / "post-replay.db"); init_schema(conn)
    run_id = _run(conn); gh = RecordingGh()
    policy = RehearsalPostingPolicy(active=True, allow_create=True)
    first = _post_run_locked(run_id, conn=conn, gh=gh, slack=object(), rehearsal_policy=policy)
    assert first["posted"] and len(gh.mutations) == 1 and gh.mutations[0][3] == []
    operation = conn.execute("SELECT * FROM github_post_operation").fetchone()
    assert operation["body_hash"] and '"canonical_repo":"acme/api"' in operation["identity_json"]
    assert '"pr_number":7' in operation["identity_json"] and '"policy_review_identity":"policy-identity"' in operation["identity_json"]
    second = _post_run_locked(run_id, conn=conn, gh=gh, slack=object(), rehearsal_policy=policy)
    assert second == {"operation_id": operation["id"], "replayed": True}
    assert len(gh.mutations) == 1


def test_multi_vendor_replay_uses_documented_plural_noop_contract(tmp_path):
    conn = connect(tmp_path / "multi.db"); init_schema(conn)
    run_id = _run(conn, vendors=("claude", "codex")); gh = RecordingGh()
    policy = RehearsalPostingPolicy(active=True, allow_create=True)
    first = _post_run_locked(
        run_id, conn=conn, gh=gh, slack=None, rehearsal_policy=policy
    )
    assert len(first["posted"]) == 2
    replay = _post_run_locked(
        run_id, conn=conn, gh=gh, slack=None, rehearsal_policy=policy
    )
    assert replay["replayed"] is True
    assert len(replay["operation_ids"]) == 2
    assert len(gh.mutations) == 2


def test_legacy_operation_identity_is_not_treated_as_safe_replay(tmp_path):
    conn = connect(tmp_path / "legacy.db"); init_schema(conn)
    run_id = _run(conn); gh = RecordingGh()
    conn.execute("UPDATE finding SET status='posted' WHERE run_id=?", (run_id,))
    conn.execute(
        """INSERT INTO github_post_operation
           (operation_key, run_id, vendor, marker, body, finding_ids,
            new_finding_ids, status, created_at, updated_at)
           VALUES ('legacy', ?, 'claude', '<!-- legacy -->', 'body', '[]',
                   '[]', 'succeeded', datetime('now'), datetime('now'))""",
        (run_id,),
    )
    conn.commit()
    try:
        _post_run_locked(run_id, conn=conn, gh=gh, slack=None)
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "legacy" in str(exc.detail)
    else:
        raise AssertionError("legacy operation was accepted as exact replay")
