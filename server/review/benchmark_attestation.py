"""Fail-closed local binding for sanitized benchmark rollout evidence.

This is intentionally not a signature verifier.  It binds an operator-pinned,
canonical local report to the exact checked-out implementation identity and only
returns public identifiers; report paths and contents never leave this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from scripts.review_benchmark_common import (
    BenchmarkError,
    bernoulli_metric,
    canonical_json,
    validate_schema,
)
from server import config
from server.review.rollout import evaluate_scope_dedupe_rollout


@dataclass(frozen=True)
class BenchmarkRuntimeIdentity:
    vendor: str
    model: str
    effort: str
    prompt_sha256: str
    protocol_sha256: str
    chunker_sha256: str
    chunk_budget: int
    adapter_sha256: str
    cli_version: str
    event_schema_sha256: str


@dataclass(frozen=True)
class BenchmarkIdentity:
    implementation_commit_sha: str
    vendor: str
    model: str
    effort: str
    prompt_sha256: str
    protocol_sha256: str
    chunker_sha256: str
    chunk_budget: int
    adapter_sha256: str
    cli_version: str
    event_schema_sha256: str
    corpus_manifest_sha256: str
    adjudication_commitment_sha256: str
    primary_run_selection_sha256: str
    paired_schedule_sha256: str
    scorer_sha256: str
    schema_sha256: str


class AttestationReason(StrEnum):
    VALID = "valid"
    MISSING_REPORT_PATH = "missing_report_path"
    MISSING_EXPECTED_HASH = "missing_expected_hash"
    MISSING_EXPECTED_IDENTITY = "missing_expected_identity"
    REPORT_UNAVAILABLE = "report_unavailable"
    REPORT_INVALID = "report_invalid"
    REPORT_HASH_MISMATCH = "report_hash_mismatch"
    INVALID_TIMESTAMP = "invalid_timestamp"
    EXPIRED = "expired"
    NOT_ENFORCEABLE = "not_enforceable"
    FAILURE_REASONS_PRESENT = "failure_reasons_present"
    CLEAN_COMMIT_UNAVAILABLE = "clean_commit_unavailable"
    IMPLEMENTATION_DIRTY = "implementation_dirty"
    IMPLEMENTATION_COMMIT_MISMATCH = "implementation_commit_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    RUNTIME_IDENTITY_MISSING = "runtime_identity_missing"
    RUNTIME_IDENTITY_MISMATCH = "runtime_identity_mismatch"


@dataclass(frozen=True)
class BenchmarkAttestationDecision:
    can_enforce: bool
    reason: AttestationReason
    report_hash: str | None
    identity: BenchmarkIdentity | None


def _decision(reason: AttestationReason, *, identity: BenchmarkIdentity | None = None,
              report_hash: str | None = None) -> BenchmarkAttestationDecision:
    return BenchmarkAttestationDecision(
        can_enforce=reason is AttestationReason.VALID,
        reason=reason,
        report_hash=report_hash if reason is AttestationReason.VALID else None,
        identity=identity,
    )


def _identity_from_report(report: dict[str, Any]) -> BenchmarkIdentity:
    identity = report["identity"]
    return BenchmarkIdentity(
        implementation_commit_sha=report["implementation_commit_sha"],
        vendor=identity["vendor"], model=identity["model"], effort=identity["effort"],
        prompt_sha256=identity["prompt_sha256"], protocol_sha256=identity["protocol_sha256"],
        chunker_sha256=identity["chunker_sha256"],
        chunk_budget=identity["chunk_budget"],
        adapter_sha256=identity["adapter_sha256"],
        cli_version=identity["cli_version"], event_schema_sha256=identity["event_schema_sha256"],
        corpus_manifest_sha256=report["corpus_manifest_sha256"],
        adjudication_commitment_sha256=report["adjudication_commitment_sha256"],
        primary_run_selection_sha256=report["primary_run_selection_sha256"],
        paired_schedule_sha256=report["paired_schedule_sha256"],
        scorer_sha256=report["scorer_sha256"], schema_sha256=report["schema_sha256"],
    )


def _report_metrics_are_consistent(report: dict[str, Any]) -> bool:
    rules = {
        "issue_precision": (0.995, 0.99, 1),
        "issue_recall": (0.95, 0.90, 1),
        "duplicate_precision": (1.0, 0.88, 30),
        "duplicate_recall": (0.95, 0.85, 30),
        "scope_accuracy": (0.995, 0.99, 1),
        "posting_accuracy": (0.995, 0.99, 1),
    }
    try:
        for name, (point, lower, minimum) in rules.items():
            metric = report["metrics"][name]
            expected = bernoulli_metric(
                metric["numerator"], metric["denominator"],
                point_threshold=point, lower_threshold=lower,
                minimum_denominator=minimum,
            )
            if metric != expected:
                return False
        cost = report["metrics"]["cost_regression"]
        numerator, denominator = cost["numerator"], cost["denominator"]
        ratio = numerator / denominator if denominator else 0.0
        expected_cost = {
            "numerator": numerator,
            "denominator": denominator,
            "point_estimate": ratio,
            "wilson_95_lower_bound": 0.0,
            "threshold": 1.10,
            "passed": denominator > 0 and ratio <= 1.10,
            "required_sample_shortfall": 0 if denominator > 0 else 1,
        }
        if cost != expected_cost:
            return False
        metrics = report["metrics"]
        evidence = report["rollout_evidence"]
        if (
            report["finding_count"] != metrics["scope_accuracy"]["denominator"]
            or report["finding_count"]
            != metrics["posting_accuracy"]["denominator"]
            or report["issue_count"] != metrics["issue_recall"]["denominator"]
            or evidence["pr_size_strata_covered"]
            > min(3, evidence["case_count"])
            or evidence["partial_timeout_cases"] > evidence["case_count"]
        ):
            return False
        gate = evaluate_scope_dedupe_rollout(report)
        if report["can_enforce"] != gate.can_enforce:
            return False
        if report["can_enforce"] and report["failure_reasons"]:
            return False
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
    return True


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _read_canonical_report(path: Path) -> tuple[dict[str, Any], str] | None:
    try:
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            return None
        raw = path.read_bytes()
        def reject_constant(_: str) -> None:
            raise ValueError("non-finite JSON number")
        report = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
        if raw != canonical_json(report):
            return None
        _reject_nonfinite(report)
        validate_schema("benchmark-report", report)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, BenchmarkError):
        return None
    return report, hashlib.sha256(raw).hexdigest()


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, dict):
        for child in value.values():
            _reject_nonfinite(child)
    elif isinstance(value, list):
        for child in value:
            _reject_nonfinite(child)


def _clean_head(repo_root: Path) -> tuple[str | None, AttestationReason | None]:
    try:
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain=v1", "--untracked-files=all"],
            text=True, capture_output=True, check=False, timeout=3,
        )
        if status.returncode != 0:
            return None, AttestationReason.CLEAN_COMMIT_UNAVAILABLE
        if status.stdout:
            return None, AttestationReason.IMPLEMENTATION_DIRTY
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "HEAD^{commit}"],
            text=True, capture_output=True, check=False, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, AttestationReason.CLEAN_COMMIT_UNAVAILABLE
    value = head.stdout.strip().lower()
    if head.returncode or len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        return None, AttestationReason.CLEAN_COMMIT_UNAVAILABLE
    return value, None


def resolve_benchmark_attestation(
    *, report_path: Path | None = None, expected_hash: str | None = None,
    expected_identity: BenchmarkIdentity | None = None,
    runtime_identity: BenchmarkRuntimeIdentity | dict[str, Any] | None = None,
    repo_root: Path | None = None, now: datetime | None = None,
) -> BenchmarkAttestationDecision:
    """Return only a safe allow/deny reason and public identity metadata."""
    report_path = config.REVIEW_BENCHMARK_REPORT_PATH if report_path is None else report_path
    expected_hash = config.REVIEW_BENCHMARK_ATTESTATION_HASH if expected_hash is None else expected_hash
    expected_identity = config.REVIEW_BENCHMARK_EXPECTED_IDENTITY if expected_identity is None else expected_identity
    repo_root = config.BASE_DIR if repo_root is None else repo_root
    if report_path is None:
        return _decision(AttestationReason.MISSING_REPORT_PATH)
    if not expected_hash:
        return _decision(AttestationReason.MISSING_EXPECTED_HASH)
    if expected_identity is None:
        return _decision(AttestationReason.MISSING_EXPECTED_IDENTITY)
    if isinstance(expected_identity, dict):
        try:
            expected_identity = BenchmarkIdentity(**expected_identity)
        except TypeError:
            return _decision(AttestationReason.MISSING_EXPECTED_IDENTITY)
    loaded = _read_canonical_report(report_path)
    if loaded is None:
        return _decision(AttestationReason.REPORT_UNAVAILABLE)
    report, report_hash = loaded
    try:
        identity = _identity_from_report(report)
    except (KeyError, TypeError):
        return _decision(AttestationReason.REPORT_INVALID)
    if report_hash != expected_hash:
        return _decision(AttestationReason.REPORT_HASH_MISMATCH, identity=identity)
    generated = _parse_timestamp(report.get("generated_at"))
    valid_until = _parse_timestamp(report.get("valid_until"))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if generated is None or valid_until is None or generated > valid_until or generated > current:
        return _decision(AttestationReason.INVALID_TIMESTAMP, identity=identity)
    if valid_until <= current:
        return _decision(AttestationReason.EXPIRED, identity=identity)
    if not _report_metrics_are_consistent(report):
        return _decision(AttestationReason.REPORT_INVALID, identity=identity)
    if not report.get("can_enforce", False):
        return _decision(AttestationReason.NOT_ENFORCEABLE, identity=identity)
    if report.get("failure_reasons"):
        return _decision(AttestationReason.FAILURE_REASONS_PRESENT, identity=identity)
    head, clean_reason = _clean_head(repo_root)
    if clean_reason is not None:
        return _decision(clean_reason, identity=identity)
    if identity.implementation_commit_sha != head:
        return _decision(AttestationReason.IMPLEMENTATION_COMMIT_MISMATCH, identity=identity)
    if identity != expected_identity:
        return _decision(AttestationReason.IDENTITY_MISMATCH, identity=identity)
    if runtime_identity is None:
        return _decision(AttestationReason.RUNTIME_IDENTITY_MISSING, identity=identity)
    if isinstance(runtime_identity, dict):
        try:
            runtime_identity = BenchmarkRuntimeIdentity(**runtime_identity)
        except TypeError:
            return _decision(AttestationReason.RUNTIME_IDENTITY_MISMATCH, identity=identity)
    if not isinstance(runtime_identity, BenchmarkRuntimeIdentity) or any(
        getattr(identity, field) != getattr(runtime_identity, field)
        for field in BenchmarkRuntimeIdentity.__dataclass_fields__
    ):
        return _decision(AttestationReason.RUNTIME_IDENTITY_MISMATCH, identity=identity)
    return _decision(AttestationReason.VALID, identity=identity, report_hash=report_hash)
