#!/usr/bin/env python3
"""Run the deterministic, synthetic scope/dedupe rollout benchmark.

This command never invokes an external model and never reads repository source files.
External-model quality runs must use a separate, explicit opt-in workflow with scrubbed
fixtures so labels cannot be exposed to the model under test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.models import Finding  # noqa: E402
from server.pipeline import (  # noqa: E402
    PromptChunk,
    VendorRunResult,
    _apply_finding_scope,
    _group_duplicate_candidates,
)
from server.review.diff_filter import chunk_records_by_budget  # noqa: E402
from server.review.rollout import evaluate_scope_dedupe_rollout  # noqa: E402

DEFAULT_FIXTURE = ROOT / "benchmarks/review_pipeline/fixtures/synthetic_scope_dedupe.json"


def _materialize(case: dict) -> tuple[list[VendorRunResult], list[PromptChunk], list[Finding]]:
    records = chunk_records_by_budget(case["diff"], int(case["chunk_max_chars"]))
    prompt_chunks = [
        PromptChunk(
            index=item.index,
            prompt="",
            diff_text=item.text,
            diff_hash=item.diff_hash,
            context_hash=hashlib.sha256(b"").hexdigest(),
            owned_changed_lines=item.owned_changed_lines,
        )
        for item in records
    ]
    findings = []
    by_vendor: dict[str, list[Finding]] = {}
    for raw in case["findings"]:
        finding = Finding(
            vendor=raw["vendor"],
            file=raw["file"],
            line=raw["line"],
            severity=raw["severity"],
            category=raw["category"],
            claim=raw["claim"],
            rationale=raw["rationale"],
            confidence=raw["confidence"],
            source_chunk_index=raw["source_chunk_index"],
        )
        findings.append(finding)
        by_vendor.setdefault(finding.vendor, []).append(finding)
    results = [
        VendorRunResult(
            vendor=vendor,
            status="done",
            findings=vendor_findings,
            duration_ms=0,
            chunks=[
                {
                    "index": chunk.index,
                    "status": "done",
                    "scope_rejected": 0,
                    "scope_reassigned": 0,
                    "duplicate_groups": 0,
                }
                for chunk in prompt_chunks
            ],
        )
        for vendor, vendor_findings in sorted(by_vendor.items())
    ]
    return results, prompt_chunks, findings


def _duplicate_pairs(findings: list[Finding]) -> set[tuple[int, int]]:
    groups: dict[int, list[int]] = {}
    for index, finding in enumerate(findings):
        if finding.duplicate_group_id is not None:
            groups.setdefault(finding.duplicate_group_id, []).append(index)
    return {
        (left, right)
        for indices in groups.values()
        for offset, left in enumerate(indices)
        for right in indices[offset + 1 :]
    }


def run_fixture(path: Path) -> dict:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("contains_proprietary_code") is not False:
        raise ValueError("benchmark fixture must explicitly be non-proprietary")
    case_reports = []
    scope_total = scope_correct = posting_total = posting_correct = 0
    pair_tp = pair_fp = pair_fn = 0
    for case in fixture["cases"]:
        observe_results, chunks, observed = _materialize(case)
        _apply_finding_scope(observe_results, chunks, mode="observe")
        _group_duplicate_candidates(observe_results, mode="observe")
        expected_scope = case["expected_scope_status"]
        scope_total += len(expected_scope)
        scope_correct += sum(
            finding.scope_status == expected
            for finding, expected in zip(observed, expected_scope)
        )
        expected_pairs = {tuple(pair) for pair in case["expected_duplicate_pairs"]}
        actual_pairs = _duplicate_pairs(observed)
        pair_tp += len(expected_pairs & actual_pairs)
        pair_fp += len(actual_pairs - expected_pairs)
        pair_fn += len(expected_pairs - actual_pairs)

        enforce_results, enforce_chunks, enforced = _materialize(case)
        _apply_finding_scope(enforce_results, enforce_chunks, mode="enforce")
        _group_duplicate_candidates(enforce_results, mode="enforce")
        enforced = [
            finding
            for result in enforce_results
            for finding in result.findings
        ]
        expected_posting = case["expected_enforce_posting"]
        posting_total += len(expected_posting)
        posting_correct += sum(
            bool(finding.posting_eligible) == expected
            for finding, expected in zip(enforced, expected_posting)
        )
        case_reports.append(
            {
                "name": case["name"],
                "scope_status": [item.scope_status for item in observed],
                "duplicate_pairs": [list(pair) for pair in sorted(actual_pairs)],
                "enforce_posting": [bool(item.posting_eligible) for item in enforced],
            }
        )
    precision = pair_tp / (pair_tp + pair_fp) if pair_tp + pair_fp else 1.0
    recall = pair_tp / (pair_tp + pair_fn) if pair_tp + pair_fn else 1.0
    metrics = {
        "scope_accuracy": scope_correct / scope_total if scope_total else 1.0,
        "enforce_posting_accuracy": posting_correct / posting_total if posting_total else 1.0,
        "duplicate_pair_precision": precision,
        "duplicate_pair_recall": recall,
        "finding_count": scope_total,
    }
    decision = evaluate_scope_dedupe_rollout(metrics)
    return {
        "schema_version": 1,
        "fixture_schema_version": fixture["schema_version"],
        "external_model_invoked": False,
        "labels_exposed_to_model": False,
        "metrics": metrics,
        "rollout": {
            "can_enforce": decision.can_enforce,
            "reasons": list(decision.reasons),
            "next_step": "collect_adjudicated_non_proprietary_samples_then_enable_repo_canary",
        },
        "cases": case_reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_fixture(args.fixture)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    metrics = report["metrics"]
    return 0 if all(value == 1.0 for key, value in metrics.items() if key != "finding_count") else 1


if __name__ == "__main__":
    raise SystemExit(main())
