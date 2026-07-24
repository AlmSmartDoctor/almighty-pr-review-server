"""Offline contract tests for the separately-approved sandbox rehearsal.

No test in this module constructs a real GitHub transport, vendor adapter, or listener.
"""
import hashlib
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from server.github.gh import GhClient


_spec = importlib.util.spec_from_file_location(
    "sandbox_e2e", Path(__file__).parents[1] / "scripts" / "sandbox-e2e.py"
)
sandbox = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(sandbox)

_CREDENTIAL = "provisioned-credential"


def e2e_state_message(conn):
    jobs = [
        dict(row) for row in conn.execute(
            """SELECT id, pr_id, head_sha, trigger, status, attempts, error,
                      next_run_at, run_id FROM review_job ORDER BY id"""
        )
    ]
    runs = [
        dict(row) for row in conn.execute(
            "SELECT id, pr_id, head_sha, status, error FROM review_run ORDER BY id"
        )
    ]
    return f"review_job rows={jobs}; review_run rows={runs}"


def _manifest(tmp_path, allowlist_hash):
    workspace = tmp_path / "almighty-e2e-run"
    workspace.mkdir(mode=0o700, exist_ok=True)
    attestation = {
        "fingerprint": sandbox.credential_fingerprint(_CREDENTIAL),
        "installation_id": "42",
        "permissions": {"pull_requests": "read", "contents": "read"},
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(),
        "target": "acme/api#7",
        "allowlist_hash": allowlist_hash,
    }
    attestation_hash = sandbox.sha256_json(attestation)
    manifest = {
        "repo": "Acme/API",
        "pr": 7,
        "head_sha": "abc123",
        "vendor": "codex",
        "model": "test-codex",
        "phase": "review",
        "allow_post": False,
        "allowlist_hash": allowlist_hash,
        "credential_attestation_hash": attestation_hash,
        "db_path": str(workspace / "e2e.db"),
    }
    return manifest, attestation


def _validate(manifest, attestation, allowlist, tmp_path, *, credential=_CREDENTIAL):
    return sandbox.validate_manifest(
        manifest,
        allowlist_path=allowlist,
        production_db=tmp_path / "almighty.db",
        credential=credential,
        credential_attestation=attestation,
        attestation_hash=manifest["credential_attestation_hash"],
    )


def test_preflight_requires_exact_target_and_never_falls_back_to_open_list(
    tmp_path,
):
    allowlist = tmp_path / "operator-allowlist.json"
    allowlist.write_text('["acme/api#7"]')
    digest = hashlib.sha256(allowlist.read_bytes()).hexdigest()
    manifest, attestation = _manifest(tmp_path, digest)
    evidence = _validate(manifest, attestation, allowlist, tmp_path)
    assert evidence["target"] == "acme/api#7"
    assert evidence["live"] == "not_run"

    manifest["pr"] = 8
    with pytest.raises(sandbox.PreflightError, match="allowlist"):
        _validate(manifest, attestation, allowlist, tmp_path)


def test_preflight_binds_actual_credential_and_rejects_mismatch_expiry_write(
    tmp_path,
):
    allowlist = tmp_path / "operator-allowlist.json"
    allowlist.write_text('{"targets":["acme/api#7"]}')
    digest = hashlib.sha256(allowlist.read_bytes()).hexdigest()

    manifest, attestation = _manifest(tmp_path, digest)
    with pytest.raises(sandbox.PreflightError, match="fingerprint"):
        _validate(
            manifest, attestation, allowlist, tmp_path,
            credential="different-credential",
        )

    manifest, attestation = _manifest(tmp_path, digest)
    attestation["expires_at"] = "2000-01-01T00:00:00+00:00"
    manifest["credential_attestation_hash"] = sandbox.sha256_json(attestation)
    with pytest.raises(sandbox.PreflightError, match="expired"):
        _validate(manifest, attestation, allowlist, tmp_path)

    manifest, attestation = _manifest(tmp_path, digest)
    attestation["permissions"]["pull_requests"] = "write"
    manifest["credential_attestation_hash"] = sandbox.sha256_json(attestation)
    with pytest.raises(sandbox.PreflightError, match="write-capable"):
        _validate(manifest, attestation, allowlist, tmp_path)


def test_preflight_rejects_allowlist_hash_drift(tmp_path):
    allowlist = tmp_path / "operator-allowlist.json"
    allowlist.write_text('["acme/api#7"]')
    digest = hashlib.sha256(allowlist.read_bytes()).hexdigest()
    manifest, attestation = _manifest(tmp_path, digest)
    manifest["allowlist_hash"] = "wrong"
    with pytest.raises(sandbox.PreflightError, match="allowlist"):
        _validate(manifest, attestation, allowlist, tmp_path)


def test_preflight_requires_new_private_disposable_db_and_valid_phase(tmp_path):
    with pytest.raises(sandbox.PreflightError, match="private disposable"):
        sandbox.validate_db_path(tmp_path / "almighty.db", tmp_path / "almighty.db")

    workspace = tmp_path / "almighty-e2e-existing"
    workspace.mkdir(mode=0o700)
    existing = workspace / "customer-live.db"
    existing.write_bytes(b"existing")
    with pytest.raises(sandbox.PreflightError, match="new disposable"):
        sandbox.validate_db_path(existing, tmp_path / "almighty.db")

    arbitrary = tmp_path / "customer-live.db"
    with pytest.raises(sandbox.PreflightError, match="private disposable"):
        sandbox.validate_db_path(arbitrary, tmp_path / "almighty.db")

    with pytest.raises(sandbox.PreflightError, match="allow-post"):
        sandbox.validate_phase("review", allow_post=True)
    with pytest.raises(sandbox.PreflightError, match="requires explicit"):
        sandbox.validate_phase("post-verify", allow_post=False)


def test_isolated_environment_binds_token_strips_ambient_and_cleans(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ambient")
    expected = sandbox.credential_fingerprint(_CREDENTIAL)
    with sandbox.isolated_gh_environment(
        _CREDENTIAL,
        expected_fingerprint=expected,
        base_env=dict(os.environ),
    ) as env:
        directory = Path(env["GH_CONFIG_DIR"])
        assert directory.exists() and stat_mode(directory) == 0o700
        assert env["GH_TOKEN"] == _CREDENTIAL
        assert "GITHUB_TOKEN" not in env
        seen = []
        GhClient(
            runner=lambda args, **kwargs: seen.append(kwargs["env"]) or "[]",
            env=env,
            strict_isolated=True,
        ).list_pr_reviews("acme/api", 7)
        assert seen[0]["GH_TOKEN"] == _CREDENTIAL
        assert "GITHUB_TOKEN" not in seen[0]
    assert not directory.exists()


def test_strict_gh_client_rejects_extra_token_and_nonprivate_config(tmp_path):
    config_dir = tmp_path / "gh"
    config_dir.mkdir(mode=0o700)
    base = {"GH_CONFIG_DIR": str(config_dir), "GH_TOKEN": "isolated"}
    with pytest.raises(RuntimeError, match="ambient tokens"):
        GhClient(
            runner=lambda *args, **kwargs: "[]",
            env={**base, "GITHUB_TOKEN": "ambient"},
            strict_isolated=True,
        ).list_pr_reviews("acme/api", 7)
    config_dir.chmod(0o755)
    with pytest.raises(RuntimeError, match="0700"):
        GhClient(
            runner=lambda *args, **kwargs: "[]",
            env=base,
            strict_isolated=True,
        ).list_pr_reviews("acme/api", 7)


def stat_mode(path):
    return path.stat().st_mode & 0o777
