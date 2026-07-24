import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from server import config
from server.review.benchmark_attestation import (
    BenchmarkAttestationDecision,
    resolve_benchmark_attestation,
)


def normalize_finding_path(value: str) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None

    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts:
        return None
    normalized = parsed.as_posix()
    return normalized if normalized not in {"", "."} else None


def apply_finding_scope(results, prompt_chunks, *, mode: str) -> None:
    owner = {}
    for chunk in prompt_chunks:
        for path, lines in chunk.owned_changed_lines.items():
            for line in lines:
                key = (path, line)
                if key in owner:
                    raise ValueError("changed line has multiple chunk owners")
                owner[key] = chunk.index
    for result in results:
        kept = []
        chunk_meta = {chunk["index"]: chunk for chunk in result.chunks}
        for finding in result.findings:
            normalized = normalize_finding_path(finding.file)
            finding.file = normalized or finding.file
            owner_index = owner.get((normalized, finding.line)) if normalized else None
            finding.owner_chunk_index = owner_index
            source_index = finding.source_chunk_index
            if owner_index is None:
                finding.scope_status = (
                    "rejected" if mode == "enforce" else "would_reject"
                )
                finding.posting_eligible = False
                if source_index in chunk_meta:
                    chunk_meta[source_index]["scope_rejected"] += 1
                if mode == "enforce":
                    continue
            elif owner_index == source_index:
                finding.scope_status = "owned"
                finding.posting_eligible = True
            else:
                finding.scope_status = "reassigned"
                finding.posting_eligible = True
                if source_index in chunk_meta:
                    chunk_meta[source_index]["scope_reassigned"] += 1
            kept.append(finding)
        result.findings = kept


def canonical_claim(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\W_]+", " ", normalized).strip()


def duplicate_key(finding) -> tuple:
    return (
        finding.vendor if hasattr(finding, "vendor") else finding["vendor"],
        normalize_finding_path(
            finding.file if hasattr(finding, "file") else finding["file"]
        ),
        finding.line if hasattr(finding, "line") else finding["line"],
        finding.category if hasattr(finding, "category") else finding["category"],
        canonical_claim(
            finding.claim if hasattr(finding, "claim") else finding["claim"]
        ),
    )


def group_duplicate_candidates(results, *, mode: str) -> None:
    grouped = {}
    for result in results:
        for finding in result.findings:
            grouped.setdefault(duplicate_key(finding), []).append(finding)
    group_id = 0
    chunk_meta = {
        (result.vendor, chunk["index"]): chunk
        for result in results
        for chunk in result.chunks
    }
    for candidates in grouped.values():
        if len(candidates) < 2:
            continue
        group_id += 1
        for index, finding in enumerate(candidates):
            finding.duplicate_group_id = group_id
            finding.duplicate_suggested = True
            meta = chunk_meta.get((finding.vendor, finding.source_chunk_index))
            if meta is not None:
                meta["duplicate_groups"] += 1
            if mode == "enforce" and index > 0:
                finding.posting_eligible = False


@dataclass(frozen=True)
class PolicyDecision:
    policy: str
    requested_mode: str
    effective_mode: str
    reason: str
    selection_source: str


@dataclass(frozen=True)
class PolicySnapshot:
    scope: PolicyDecision
    dedupe: PolicyDecision
    cohort_key: str
    decision_hash: str
    config_hash: str
    benchmark_attestation_hash: str | None


_POLICY_SNAPSHOT_COLUMNS = (
    "scope_requested_mode", "scope_effective_mode", "scope_policy_reason",
    "scope_selection_source", "dedupe_requested_mode", "dedupe_effective_mode",
    "dedupe_policy_reason", "dedupe_selection_source", "policy_cohort_key",
    "policy_decision_hash", "policy_config_hash", "benchmark_attestation_hash",
)


def _row_value(row, key, default=None):
    if row is None:
        return default
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def _canonical_hash(value) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_policy_decision(
    repo, *, policy: str, default_mode: str | None = None,
    benchmark_attestation: BenchmarkAttestationDecision | None = None,
) -> PolicyDecision:
    if policy == "scope":
        column = "review_scope_guard_mode"
        default = config.REVIEW_SCOPE_GUARD_MODE
        canaries = config.REVIEW_SCOPE_ENFORCE_REPOS
        kill_switch = config.REVIEW_SCOPE_KILL_SWITCH
    elif policy == "dedupe":
        column = "review_dedupe_mode"
        default = config.REVIEW_DEDUPE_MODE
        canaries = config.REVIEW_DEDUPE_ENFORCE_REPOS
        kill_switch = config.REVIEW_DEDUPE_KILL_SWITCH
    else:
        raise ValueError(f"invalid policy: {policy!r}")

    if default_mode is not None:
        default = default_mode
    explicit_value = _row_value(repo, column)
    explicit = explicit_value if explicit_value in {"observe", "enforce"} else None
    requested = explicit or default
    if requested not in {"observe", "enforce"}:
        requested = "observe"
    source = "repo_override" if explicit is not None else "global_default"
    full_name = _row_value(repo, "full_name", "") or ""

    if requested != "enforce":
        effective, reason = "observe", "requested"
    elif kill_switch:
        effective, reason = "observe", "kill_switch"
    elif not config.REVIEW_POLICY_ENFORCEMENT_UNLOCKED:
        effective, reason = "observe", "benchmark_gate_locked"
    elif not (benchmark_attestation or resolve_benchmark_attestation()).can_enforce:
        effective, reason = "observe", "benchmark_gate_locked"
    elif explicit == "enforce":
        effective, reason = "enforce", "repo_canary"
    elif full_name in canaries:
        effective, reason = "enforce", "global_canary"
    else:
        effective, reason = "observe", "not_in_global_canary"
    return PolicyDecision(
        policy=policy,
        requested_mode=requested,
        effective_mode=effective,
        reason=reason,
        selection_source=source,
    )


def resolve_policy_snapshot(repo) -> PolicySnapshot:
    benchmark_attestation = resolve_benchmark_attestation()
    scope = resolve_policy_decision(
        repo, policy="scope", benchmark_attestation=benchmark_attestation
    )
    dedupe = resolve_policy_decision(
        repo, policy="dedupe", benchmark_attestation=benchmark_attestation
    )
    decision_payload = {"scope": asdict(scope), "dedupe": asdict(dedupe)}
    decision_hash = _canonical_hash(decision_payload)
    full_name = _row_value(repo, "full_name", "") or ""
    relevant_config = {
        "version": "policy-config-v1",
        "repo": full_name,
        "scope_override": _row_value(repo, "review_scope_guard_mode"),
        "dedupe_override": _row_value(repo, "review_dedupe_mode"),
        "scope_default": config.REVIEW_SCOPE_GUARD_MODE,
        "dedupe_default": config.REVIEW_DEDUPE_MODE,
        "scope_global_canary": full_name in config.REVIEW_SCOPE_ENFORCE_REPOS,
        "dedupe_global_canary": full_name in config.REVIEW_DEDUPE_ENFORCE_REPOS,
        "enforcement_unlocked": config.REVIEW_POLICY_ENFORCEMENT_UNLOCKED,
        "scope_kill_switch": config.REVIEW_SCOPE_KILL_SWITCH,
        "dedupe_kill_switch": config.REVIEW_DEDUPE_KILL_SWITCH,
        "benchmark_attestation_hash": benchmark_attestation.report_hash,
        "decisions": decision_payload,
    }
    cohort_key = (
        f"scope={scope.selection_source}:{scope.effective_mode};"
        f"dedupe={dedupe.selection_source}:{dedupe.effective_mode}"
    )
    return PolicySnapshot(
        scope=scope,
        dedupe=dedupe,
        cohort_key=cohort_key,
        decision_hash=decision_hash,
        config_hash=_canonical_hash(relevant_config),
        benchmark_attestation_hash=benchmark_attestation.report_hash,
    )


def policy_snapshot_record(snapshot: PolicySnapshot) -> dict:
    return {
        "scope_requested_mode": snapshot.scope.requested_mode,
        "scope_effective_mode": snapshot.scope.effective_mode,
        "scope_policy_reason": snapshot.scope.reason,
        "scope_selection_source": snapshot.scope.selection_source,
        "dedupe_requested_mode": snapshot.dedupe.requested_mode,
        "dedupe_effective_mode": snapshot.dedupe.effective_mode,
        "dedupe_policy_reason": snapshot.dedupe.reason,
        "dedupe_selection_source": snapshot.dedupe.selection_source,
        "policy_cohort_key": snapshot.cohort_key,
        "policy_decision_hash": snapshot.decision_hash,
        "policy_config_hash": snapshot.config_hash,
        "benchmark_attestation_hash": snapshot.benchmark_attestation_hash,
    }


def policy_snapshot_from_row(row) -> PolicySnapshot | None:
    record = {key: _row_value(row, key) for key in _POLICY_SNAPSHOT_COLUMNS}
    required = {key for key in record if key != "benchmark_attestation_hash"}
    if any(not isinstance(record[key], str) or not record[key] for key in required):
        return None
    scope = PolicyDecision(
        "scope",
        record["scope_requested_mode"],
        record["scope_effective_mode"],
        record["scope_policy_reason"],
        record["scope_selection_source"],
    )
    dedupe = PolicyDecision(
        "dedupe",
        record["dedupe_requested_mode"],
        record["dedupe_effective_mode"],
        record["dedupe_policy_reason"],
        record["dedupe_selection_source"],
    )
    if record["policy_decision_hash"] != _canonical_hash(
        {"scope": asdict(scope), "dedupe": asdict(dedupe)}
    ):
        return None
    if any(
        decision.requested_mode not in {"observe", "enforce"}
        or decision.effective_mode not in {"observe", "enforce"}
        or decision.selection_source not in {"repo_override", "global_default"}
        for decision in (scope, dedupe)
    ):
        return None
    expected_cohort = (
        f"scope={scope.selection_source}:{scope.effective_mode};"
        f"dedupe={dedupe.selection_source}:{dedupe.effective_mode}"
    )
    if record["policy_cohort_key"] != expected_cohort:
        return None
    for key in ("policy_decision_hash", "policy_config_hash"):
        value = record[key]
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            return None
    attestation = record["benchmark_attestation_hash"]
    if attestation is not None and (
        not isinstance(attestation, str)
        or len(attestation) != 64
        or any(ch not in "0123456789abcdef" for ch in attestation)
    ):
        return None
    return PolicySnapshot(
        scope=scope,
        dedupe=dedupe,
        cohort_key=record["policy_cohort_key"],
        decision_hash=record["policy_decision_hash"],
        config_hash=record["policy_config_hash"],
        benchmark_attestation_hash=record["benchmark_attestation_hash"],
    )


def policy_mode(repo, key: str, default: str, *, policy: str) -> str:
    """Compatibility wrapper; all semantics live in ``resolve_policy_decision``."""
    return resolve_policy_decision(
        repo, policy=policy, default_mode=default
    ).effective_mode


def apply_scope_and_duplicate_policy(
    results, prompt_chunks, *, repo=None, snapshot: PolicySnapshot | None = None
) -> None:
    current = snapshot or resolve_policy_snapshot(repo)
    apply_finding_scope(
        results, prompt_chunks, mode=current.scope.effective_mode
    )
    group_duplicate_candidates(results, mode=current.dedupe.effective_mode)
