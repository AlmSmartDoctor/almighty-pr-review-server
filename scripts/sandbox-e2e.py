#!/usr/bin/env python3
"""Fail-closed, offline-testable guards for the separately-approved sandbox rehearsal.

This tool deliberately has no live default: it validates a supplied manifest and emits only
sanitized evidence.  A future approved executor must supply its own read-only transport.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

TOKEN_ENV_NAMES = frozenset({
    "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN", "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
})
VALID_PHASES = frozenset({"review", "retry", "post-verify", "webhook-verify"})


class PreflightError(RuntimeError):
    pass


def canonical_repo(repo: str) -> str:
    value = (repo or "").strip().lower()
    if value.count("/") != 1 or any(not part for part in value.split("/")):
        raise PreflightError("canonical owner/repo is required")
    return value


def canonical_target(repo: str, number: int) -> str:
    if not isinstance(number, int) or number <= 0:
        raise PreflightError("exact positive PR number is required")
    return f"{canonical_repo(repo)}#{number}"


def sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_allowlist(path: str | Path) -> tuple[frozenset[str], str]:
    """Read an operator-owned immutable list; runners never create or mutate it."""
    source = Path(path)
    if not source.is_file():
        raise PreflightError("operator allowlist file is required")
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PreflightError("operator allowlist must be JSON") from exc
    entries = data.get("targets") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise PreflightError("operator allowlist targets must be a list")
    normalized = frozenset(canonical_target(*_split_target(item)) for item in entries)
    if len(normalized) != len(entries):
        raise PreflightError("operator allowlist contains duplicate/invalid targets")
    return normalized, digest


def _split_target(value: Any) -> tuple[str, int]:
    if not isinstance(value, str) or "#" not in value:
        raise PreflightError("allowlist entry must be owner/repo#PR")
    repo, number = value.rsplit("#", 1)
    try:
        return repo, int(number)
    except ValueError as exc:
        raise PreflightError("allowlist PR must be numeric") from exc


def credential_fingerprint(credential: str) -> str:
    if not credential:
        raise PreflightError("isolated credential is required")
    return "sha256:" + hashlib.sha256(credential.encode()).hexdigest()


def validate_attestation(attestation: dict[str, Any], *, target: str, allowlist_hash: str,
                         credential: str, now: datetime | None = None,
                         require_read_only: bool = True) -> None:
    required = ("fingerprint", "installation_id", "permissions", "expires_at", "target", "allowlist_hash")
    if not isinstance(attestation, dict) or any(not attestation.get(key) for key in required):
        raise PreflightError("incomplete credential attestation")
    if attestation["target"] != target or attestation["allowlist_hash"] != allowlist_hash:
        raise PreflightError("credential target or allowlist attestation mismatch")
    if attestation["fingerprint"] != credential_fingerprint(credential):
        raise PreflightError("credential fingerprint binding mismatch")
    try:
        expiry = datetime.fromisoformat(str(attestation["expires_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreflightError("credential attestation expiry is invalid") from exc
    now = now or datetime.now(timezone.utc)
    if expiry.tzinfo is None:
        raise PreflightError("credential expiry must include timezone")
    if expiry <= now:
        raise PreflightError("credential attestation is expired")
    permissions = attestation["permissions"]
    if not isinstance(permissions, dict):
        raise PreflightError("credential permissions must be attested")
    writes = {name for name, level in permissions.items() if str(level).lower() in {"write", "admin", "maintain"}}
    if require_read_only and writes:
        raise PreflightError("read rehearsal rejects write-capable credential")
    if require_read_only and not any(str(level).lower() == "read" for level in permissions.values()):
        raise PreflightError("read capability is not independently attested")


def validate_db_path(db_path: str | Path, production_db: str | Path) -> Path:
    raw_candidate = Path(db_path).expanduser()
    if raw_candidate.exists() or raw_candidate.is_symlink():
        raise PreflightError("sandbox DB must be a new disposable file")
    candidate = raw_candidate.resolve()
    production = Path(production_db).expanduser().resolve()
    parent = candidate.parent
    try:
        parent_mode = parent.stat().st_mode & 0o777
    except OSError as exc:
        raise PreflightError("sandbox DB private workspace is required") from exc
    if (
        candidate == production or candidate.name == "almighty.db"
        or not parent.is_dir() or parent_mode != 0o700
        or not parent.name.startswith("almighty-e2e-")
    ):
        raise PreflightError("sandbox DB must use a new private disposable workspace")
    return candidate


def validate_phase(phase: str, *, allow_post: bool) -> None:
    if phase not in VALID_PHASES:
        raise PreflightError("invalid rehearsal phase")
    if allow_post and phase != "post-verify":
        raise PreflightError("--allow-post is valid only for post-verify")
    if phase == "post-verify" and not allow_post:
        raise PreflightError("post-verify requires explicit --allow-post")


def validate_manifest(
    manifest: dict[str, Any], *, allowlist_path: str | Path,
    production_db: str | Path, credential: str,
    credential_attestation: dict[str, Any], attestation_hash: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    repo = canonical_repo(manifest.get("repo", ""))
    number = manifest.get("pr")
    target = canonical_target(repo, number)
    allowed, allowlist_hash = load_allowlist(allowlist_path)
    if target not in allowed or manifest.get("allowlist_hash") != allowlist_hash:
        raise PreflightError("exact target is not in immutable operator allowlist")
    validate_phase(manifest.get("phase", ""), allow_post=bool(manifest.get("allow_post")))
    if (
        not attestation_hash
        or manifest.get("credential_attestation_hash") != attestation_hash
        or sha256_json(credential_attestation) != attestation_hash
    ):
        raise PreflightError("credential attestation hash mismatch")
    validate_attestation(credential_attestation, target=target,
                         allowlist_hash=allowlist_hash, credential=credential, now=now,
                         require_read_only=manifest.get("phase") in {"review", "retry"})
    db_path = validate_db_path(manifest.get("db_path", ""), production_db)
    if not manifest.get("head_sha") or manifest.get("vendor") not in {"claude", "codex"} or not manifest.get("model"):
        raise PreflightError("head SHA, supported vendor, and model are required")
    return {"target": target, "allowlist_hash": allowlist_hash, "db_path": str(db_path),
            "phase": manifest["phase"], "head_sha": manifest["head_sha"],
            "vendor": manifest["vendor"], "model": manifest["model"], "live": "not_run"}


@contextmanager
def isolated_gh_environment(
    credential: str, *, expected_fingerprint: str,
    base_env: dict[str, str] | None = None,
) -> Iterator[dict[str, str]]:
    """Supply a new 0700 GH_CONFIG_DIR and one credential, never ambient/native auth."""
    if credential_fingerprint(credential) != expected_fingerprint:
        raise PreflightError("credential fingerprint binding mismatch")
    config_dir = Path(tempfile.mkdtemp(prefix="almighty-e2e-gh-"))
    os.chmod(config_dir, 0o700)
    source_env = os.environ if base_env is None else base_env
    env = {key: value for key, value in source_env.items() if key not in TOKEN_ENV_NAMES and key != "GH_CONFIG_DIR"}
    env["GH_CONFIG_DIR"] = str(config_dir)
    env["GH_TOKEN"] = credential
    active_error = None
    try:
        yield env
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        try:
            shutil.rmtree(config_dir)
            if config_dir.exists():
                raise OSError("residual directory")
        except OSError as exc:
            cleanup_error = PreflightError("isolated GH_CONFIG_DIR cleanup failed")
            if active_error is None:
                raise cleanup_error from exc
            active_error.add_note(str(cleanup_error))


def complete_page_snapshot(fetch_page, *, page_size: int = 100, max_pages: int = 100) -> list[dict]:
    """Require explicit end-of-pages; a cap or transport truncation is never comparable."""
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        response = fetch_page(page, page_size)
        if not isinstance(response, dict) or response.get("truncated"):
            raise PreflightError("pagination response is truncated")
        items = response.get("items")
        if not isinstance(items, list):
            raise PreflightError("pagination response lacks item list")
        rows.extend(items)
        if response.get("complete") is True:
            return rows
    raise PreflightError("pagination reached cap without complete marker")


def snapshot_digest(snapshot: dict[str, list[dict]]) -> str:
    required = {"reviews", "inline_comments", "conversation_comments", "head"}
    if set(snapshot) != required:
        raise PreflightError("snapshot must include all remote mutation surfaces")
    return sha256_json(snapshot)


def sanitized_evidence(preflight: dict[str, Any], *, before: dict | None = None, after: dict | None = None) -> dict[str, Any]:
    evidence = {key: preflight[key] for key in ("target", "allowlist_hash", "phase", "head_sha", "vendor", "model", "live")}
    if before is not None and after is not None:
        evidence["mutation_snapshot_match"] = snapshot_digest(before) == snapshot_digest(after)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="offline fail-closed sandbox rehearsal preflight")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--credential-attestation", required=True)
    parser.add_argument("--credential-attestation-sha256", required=True)
    parser.add_argument("--production-db", default="almighty.db")
    parser.add_argument(
        "--credential-env", default="ALMIGHTY_E2E_GH_TOKEN",
        help="environment variable containing the separately provisioned credential",
    )
    args = parser.parse_args()
    try:
        manifest = json.loads(Path(args.manifest).read_text())
        credential = os.environ.get(args.credential_env, "")
        attestation_raw = Path(args.credential_attestation).read_bytes()
        credential_attestation = json.loads(attestation_raw)
        if sha256_json(credential_attestation) != args.credential_attestation_sha256:
            raise PreflightError("credential attestation hash mismatch")
        evidence = validate_manifest(
            manifest, allowlist_path=args.allowlist,
            production_db=args.production_db, credential=credential,
            credential_attestation=credential_attestation,
            attestation_hash=args.credential_attestation_sha256,
        )
    except (OSError, ValueError, PreflightError):
        print(
            json.dumps({"status": "failed", "safe_error_code": "preflight_failed"}),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(sanitized_evidence(evidence), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
