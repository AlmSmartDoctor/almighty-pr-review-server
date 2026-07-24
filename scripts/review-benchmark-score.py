#!/usr/bin/env python3
"""Offline-only scorer for physically separated review benchmark artifacts.

The command never invokes a model or reads network state.  It joins manifest,
prediction, run, and adjudication roots only after validating their separation and
writes stable-ID-only audit evidence to a private workspace.
"""
from __future__ import annotations

import argparse
import itertools
import json
import stat
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_SCRIPT_DIR))

from server.review.diff_filter import chunk_records_by_budget  # noqa: E402
from server.review.finding_policy import (  # noqa: E402
    canonical_claim as production_canonical_claim,
    normalize_finding_path,
)
from server.review.pipeline_contracts import REVIEW_CHUNKER_VERSION  # noqa: E402
from review_benchmark_common import (  # noqa: E402
    BenchmarkError, CLAIM_NORMALIZATION_VERSION, CLAIM_TOKENIZER_SHA256, ROOT,
    assert_safe_tree, bernoulli_metric, canonical_claim_tokens, canonical_json,
    disjoint_paths, load_json, private_file, sha256_bytes, sha256_file,
    validate_schema,
)

TERMINAL = {"completed", "partial", "timeout", "failed", "not_invoked_setup_failure"}
IDENTITY_KEYS = (
    "vendor", "model", "effort", "protocol_sha256", "prompt_sha256",
    "chunker_sha256", "chunk_budget", "adapter_sha256", "cli_version",
    "event_schema_sha256",
)
METRIC_RULES = {
    "issue_precision": (0.995, 0.99, 1), "issue_recall": (0.95, 0.90, 1),
    "duplicate_precision": (1.0, 0.88, 30), "duplicate_recall": (0.95, 0.85, 30),
    "scope_accuracy": (0.995, 0.99, 1), "posting_accuracy": (0.995, 0.99, 1),
}


def _json_values(root: Path, schema: str) -> list[tuple[Path, dict[str, Any]]]:
    if root.is_symlink():
        raise BenchmarkError(f"{schema} root symlink is forbidden")
    if root.is_file():
        files = [root]
    else:
        files = [item for item in assert_safe_tree(root) if item.suffix == ".json"]
    if not files:
        raise BenchmarkError(f"no {schema} JSON artifacts")
    result = []
    for path in files:
        value = load_json(path)
        validate_schema(schema, value)
        result.append((path, value))
    return result


def _manifest_values(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    values = _json_values(root, "manifest")
    seen = set()
    for _, value in values:
        if value["case_id"] in seen:
            raise BenchmarkError("duplicate manifest case ID")
        seen.add(value["case_id"])
    return values


def _chunk_ownership(
    manifest_path: Path, manifest: dict[str, Any], chunk_budget: int
) -> tuple[dict[tuple[str, int], int], int]:
    path = manifest_path.parent / manifest["model_visible_input"]["path"]
    if (
        not path.is_file() or path.is_symlink()
        or sha256_file(path) != manifest["model_visible_input"]["sha256"]
    ):
        raise BenchmarkError("model-visible input is missing or hash mismatched")
    try:
        records = chunk_records_by_budget(
            path.read_text(encoding="utf-8"), chunk_budget
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise BenchmarkError("diff ownership recomputation failed") from exc
    ownership = {
        (file_name, line): record.index
        for record in records
        for file_name, lines in record.owned_changed_lines.items()
        for line in lines
    }
    return ownership, len(records)


def _location_matches(prediction: dict[str, Any], issue: dict[str, Any]) -> bool:
    return prediction["category"] in issue["accepted_categories"] and any(
        location["file"] == prediction["file"] and location["line_start"] <= prediction["line"] <= location["line_end"]
        for location in issue["allowed_locations"]
    )


def _resolve_prediction(
    prediction: dict[str, Any], adjudication: dict[str, Any],
    resolutions: dict[str, dict[str, Any]],
) -> str | None:
    tokens = canonical_claim_tokens(prediction.get("normalized_claim_tokens"))
    candidates = [issue["issue_id"] for issue in adjudication["issues"] if _location_matches(prediction, issue) and tokens in {
        canonical_claim_tokens(sequence) for sequence in issue["canonical_claim_rubric"]["allowed_token_sequences"]
    }]
    resolution = resolutions.get(prediction["prediction_id"])
    if len(candidates) == 1:
        if resolution and (
            resolution["status"] != "resolved"
            or resolution.get("issue_id") != candidates[0]
        ):
            raise BenchmarkError("prediction resolution conflicts with unique issue")
        return candidates[0]
    if len(candidates) > 1:
        if not resolution or resolution["status"] != "resolved" or resolution.get("issue_id") not in candidates:
            raise BenchmarkError("ambiguous prediction issue match has no explicit resolution")
        return resolution["issue_id"]
    if resolution and resolution["status"] != "unmatched":
        raise BenchmarkError("prediction resolution names an unmatched issue")
    return None


def _metric(name: str, numerator: int, denominator: int) -> dict[str, Any]:
    point, lower, minimum = METRIC_RULES[name]
    return bernoulli_metric(numerator, denominator, point_threshold=point, lower_threshold=lower, minimum_denominator=minimum)


def _cost(run: dict[str, Any]) -> int:
    # ntcu-v1: one normalized unit per input/cached-input/output/reasoning token.
    return sum(int(run.get(field, 0)) for field in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"))


def score(manifests_root: Path, predictions_root: Path, runs_root: Path, adjudication_root: Path, workspace: Path, *, implementation_commit_sha: str, generated_at: str, valid_until: str) -> dict[str, Any]:
    roots = [manifests_root, predictions_root, runs_root, adjudication_root]
    if any(root.is_symlink() for root in roots):
        raise BenchmarkError("benchmark artifact root symlink is forbidden")
    disjoint_paths(roots)
    if workspace.is_symlink() or stat.S_IMODE(workspace.stat().st_mode) != 0o700:
        raise BenchmarkError("audit workspace must be an existing 0700 directory")
    manifests = _manifest_values(manifests_root)
    predictions = _json_values(predictions_root, "prediction")
    runs = _json_values(runs_root, "run-result")
    answers = _json_values(adjudication_root, "adjudication")
    manifest_by_case = {item[1]["case_id"]: (item[0], item[1]) for item in manifests}
    answer_by_case = {item[1]["case_id"]: item[1] for item in answers}
    if len(answer_by_case) != len(answers) or set(answer_by_case) != set(manifest_by_case):
        raise BenchmarkError("adjudications must be one-to-one with manifests")
    for case_id, (path, manifest) in manifest_by_case.items():
        answer = answer_by_case[case_id]
        if answer["manifest_sha256"] != sha256_file(path):
            raise BenchmarkError("adjudication manifest hash mismatch")
        if (manifest["claim_normalization_version"], manifest["claim_tokenizer_sha256"], answer["claim_normalization_version"], answer["claim_tokenizer_sha256"]) != (CLAIM_NORMALIZATION_VERSION, CLAIM_TOKENIZER_SHA256, CLAIM_NORMALIZATION_VERSION, CLAIM_TOKENIZER_SHA256):
            raise BenchmarkError("claim normalization version or tokenizer hash mismatch")
        oracle = answer["oracle_contract"]
        ownership_hash = sha256_file(ROOT / "server/review/diff_filter.py")
        if (
            oracle["ownership_function_version"] != "diff-ownership-v1"
            or oracle["ownership_function_sha256"] != ownership_hash
            or oracle["chunker_version"] != REVIEW_CHUNKER_VERSION
            or oracle["chunker_sha256"] != ownership_hash
        ):
            raise BenchmarkError("adjudication ownership oracle identity mismatch")
    run_by_id: dict[str, dict[str, Any]] = {}
    for _, run in runs:
        if run["run_id"] in run_by_id or run["case_id"] not in manifest_by_case:
            raise BenchmarkError("duplicate or unbound run")
        manifest_path, _ = manifest_by_case[run["case_id"]]
        if run["manifest_sha256"] != sha256_file(manifest_path):
            raise BenchmarkError("run manifest hash mismatch")
        coverage_evidence = sha256_bytes(canonical_json({
            "case_id": run["case_id"], "arm": run["arm"],
            "repetition_index": run["repetition_index"],
            "seed": run["seed"], "status": run["status"],
            "schedule_sha256": run["schedule_sha256"],
        }))
        if run["coverage_evidence_sha256"] != coverage_evidence:
            raise BenchmarkError("run coverage evidence identity mismatch")
        run_by_id[run["run_id"]] = run
    predictions_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    prediction_paths_by_run: dict[str, list[Path]] = defaultdict(list)
    ids = set()
    for prediction_path, prediction in predictions:
        if prediction["prediction_id"] in ids or prediction["run_id"] not in run_by_id:
            raise BenchmarkError("duplicate or unbound prediction")
        ids.add(prediction["prediction_id"])
        run = run_by_id[prediction["run_id"]]
        if prediction["case_id"] != run["case_id"] or (prediction["claim_normalization_version"], prediction["claim_tokenizer_sha256"]) != (CLAIM_NORMALIZATION_VERSION, CLAIM_TOKENIZER_SHA256):
            raise BenchmarkError("prediction join or tokenizer mismatch")
        canonical_claim_tokens(prediction.get("normalized_claim_tokens"))
        if (
            production_canonical_claim(prediction["production_claim_key"])
            != prediction["production_claim_key"]
        ):
            raise BenchmarkError("production duplicate claim key is not canonical")
        normalized_path = normalize_finding_path(prediction["file"])
        if normalized_path is None or normalized_path != prediction["file"]:
            raise BenchmarkError("prediction path is not production-normalized")
        predictions_by_run[prediction["run_id"]].append(prediction)
        prediction_paths_by_run[prediction["run_id"]].append(prediction_path)

    for run_id, values in predictions_by_run.items():
        indices = [item["emission_index"] for item in values]
        if len(indices) != len(set(indices)):
            raise BenchmarkError("duplicate prediction emission index")

    for run_id, run in run_by_id.items():
        manifest_path, manifest = manifest_by_case[run["case_id"]]
        if run["diff_sha256"] != manifest["model_visible_input"]["sha256"]:
            raise BenchmarkError("run diff identity does not match manifest input")
        paths = sorted(
            prediction_paths_by_run.get(run_id, []), key=lambda item: item.name
        )
        if not paths:
            if run["status"] not in {"failed", "timeout", "not_invoked_setup_failure"}:
                raise BenchmarkError("invoked run is missing prediction artifacts")
            continue
        reference = Path(run["prediction_artifact_path"])
        if reference.is_absolute() or ".." in reference.parts or reference.name != paths[0].name:
            raise BenchmarkError("run prediction artifact path mismatch")
        combined = sha256_bytes(b"".join(
            len(item.name.encode("utf-8")).to_bytes(4, "big")
            + item.name.encode("utf-8")
            + bytes.fromhex(sha256_file(item))
            for item in paths
        ))
        if combined != run["prediction_artifact_sha256"]:
            raise BenchmarkError("run prediction artifact hash mismatch")

    primary: dict[str, dict[str, Any]] = {}
    for case_id in manifest_by_case:
        choices = [
            run for run in run_by_id.values()
            if run["case_id"] == case_id and run["arm"] == "candidate"
            and run["repetition_index"] == run["primary_repetition_index"]
        ]
        if len(choices) != 1:
            raise BenchmarkError("exactly one primary candidate run is required per case")
        primary[case_id] = choices[0]
    identity = {key: primary[next(iter(sorted(primary)))][key] for key in IDENTITY_KEYS}
    if any({key: run[key] for key in IDENTITY_KEYS} != identity for run in primary.values()):
        raise BenchmarkError("primary candidate identity is not deterministic")

    issue_tp: list[str] = []; issue_fp: list[str] = []; issue_fn: list[str] = []
    scope_ok: list[str] = []; scope_bad: list[str] = []; posting_ok: list[str] = []; posting_bad: list[str] = []
    duplicate_audit: list[dict[str, Any]] = []; hard_negative_audit: list[str] = []; paraphrase_negative_audit: list[str] = []
    resolved_by_prediction: dict[str, str | None] = {}
    primary_predictions: list[dict[str, Any]] = []
    scope_expected: dict[str, str] = {}
    stable_by_prediction: dict[str, str] = {}
    invalid_reasons: set[str] = set()
    for case_id, run in sorted(primary.items()):
        answer = answer_by_case[case_id]
        if run["status"] == "not_invoked_setup_failure":
            invalid_reasons.add("not_invoked_setup_failure")
            issue_fn.extend(f"{case_id}:{issue['issue_id']}" for issue in answer["issues"])
            continue
        manifest_path, manifest = manifest_by_case[case_id]
        oracle = answer["oracle_contract"]
        if run["chunker_sha256"] != oracle["chunker_sha256"]:
            raise BenchmarkError("run chunker identity does not match oracle")
        ownership, chunk_count = _chunk_ownership(
            manifest_path, manifest, run["chunk_budget"]
        )
        seen_issues: set[str] = set()
        resolutions = {
            item["prediction_id"]: item
            for item in answer["prediction_issue_resolutions"]
        }
        for prediction in sorted(predictions_by_run[run["run_id"]], key=lambda item: item["prediction_id"]):
            primary_predictions.append(prediction)
            resolved = _resolve_prediction(prediction, answer, resolutions)
            resolved_by_prediction[prediction["prediction_id"]] = resolved
            stable = f"{case_id}:{prediction['prediction_id']}"
            if resolved is None:
                issue_fp.append(stable)
            elif resolved not in seen_issues:
                issue_tp.append(f"{stable}:{resolved}"); seen_issues.add(resolved)
            else:
                duplicate_audit.append({"id": stable, "kind": "duplicate_prediction", "issue_id": resolved})
            source_chunk = prediction["source_chunk_index"]
            if source_chunk >= chunk_count:
                raise BenchmarkError("prediction source chunk is outside recomputed chunks")
            owner_chunk = ownership.get((prediction["file"], prediction["line"]))
            if owner_chunk is None:
                expected_scope = "would_reject"
            elif owner_chunk == source_chunk:
                expected_scope = "owned"
            else:
                expected_scope = "reassigned"
            scope_expected[prediction["prediction_id"]] = expected_scope
            stable_by_prediction[prediction["prediction_id"]] = stable
            (scope_ok if prediction["scope_status"] == expected_scope else scope_bad).append(stable)
        issue_fn.extend(f"{case_id}:{issue['issue_id']}" for issue in answer["issues"] if issue["issue_id"] not in seen_issues)

    exact_groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for prediction in primary_predictions:
        run = run_by_id[prediction["run_id"]]
        exact_groups[(
            prediction["case_id"], run["vendor"], prediction["file"],
            prediction["line"], prediction["category"],
            prediction["production_claim_key"],
        )].append(prediction)
    suppressed = {
        prediction["prediction_id"]
        for group in exact_groups.values()
        for prediction in sorted(group, key=lambda item: item["emission_index"])[1:]
    }
    for prediction in primary_predictions:
        prediction_id = prediction["prediction_id"]
        expected_posting = (
            "suppress"
            if scope_expected[prediction_id] == "would_reject"
            or prediction_id in suppressed
            else "post"
        )
        stable = stable_by_prediction[prediction_id]
        (posting_ok if prediction["posting_status"] == expected_posting else posting_bad).append(stable)

    duplicate_tp: list[str] = []; duplicate_fp: list[str] = []; duplicate_fn: list[str] = []
    by_universe: dict[tuple[str, str, str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for prediction in primary_predictions:
        run = run_by_id[prediction["run_id"]]
        by_universe[(
            prediction["case_id"], run["vendor"], prediction["file"],
            prediction["line"], prediction["category"],
        )].append(prediction)
    labels: dict[tuple[str, str], str] = {}
    prediction_by_id = {
        prediction["prediction_id"]: prediction
        for prediction in primary_predictions
    }
    for case_id, answer in answer_by_case.items():
        for resolution in answer["prediction_issue_resolutions"]:
            prediction_id = resolution["prediction_id"]
            if (
                prediction_id not in prediction_by_id
                or prediction_by_id[prediction_id]["case_id"] != case_id
            ):
                raise BenchmarkError(
                    "prediction resolution references unknown primary prediction"
                )
        for pair in answer["issue_pairs"]:
            pair_key = tuple(sorted((
                pair["first_prediction_id"], pair["second_prediction_id"]
            )))
            if pair_key in labels:
                raise BenchmarkError("duplicate or conflicting prediction pair label")
            if any(item not in prediction_by_id for item in pair_key):
                raise BenchmarkError("pair label references unknown primary prediction")
            if any(prediction_by_id[item]["case_id"] != case_id for item in pair_key):
                raise BenchmarkError("pair label crosses benchmark cases")
            first_issue = resolved_by_prediction[pair_key[0]]
            second_issue = resolved_by_prediction[pair_key[1]]
            if pair["label"] == "same_issue_duplicate":
                if first_issue is None or first_issue != second_issue:
                    raise BenchmarkError("same-issue pair contradicts resolved issues")
            elif first_issue is None or second_issue is None or first_issue == second_issue:
                raise BenchmarkError("hard-negative pair contradicts resolved issues")
            labels[pair_key] = pair["label"]
    encountered_pairs: set[tuple[str, str]] = set()
    for group in by_universe.values():
        for left, right in itertools.combinations(
            sorted(group, key=lambda item: item["prediction_id"]), 2
        ):
            pair_key = tuple(sorted((left["prediction_id"], right["prediction_id"])))
            encountered_pairs.add(pair_key)
            pair_id = ":".join(pair_key)
            label = labels.get(pair_key)
            equal = (
                canonical_claim_tokens(left["normalized_claim_tokens"])
                == canonical_claim_tokens(right["normalized_claim_tokens"])
            )
            true_same = label == "same_issue_duplicate"
            if label == "distinct_issue_hard_negative":
                hard_negative_audit.append(pair_id)
            if true_same and not equal:
                paraphrase_negative_audit.append(pair_id)
            if equal:
                if true_same:
                    duplicate_tp.append(pair_id)
                else:
                    duplicate_fp.append(pair_id)
            if true_same and not equal:
                duplicate_fn.append(pair_id)
    if set(labels) - encountered_pairs:
        raise BenchmarkError("labeled pair is outside duplicate candidate universe")

    metric_values = {
        "issue_precision": _metric("issue_precision", len(issue_tp), len(issue_tp) + len(issue_fp)),
        "issue_recall": _metric("issue_recall", len(issue_tp), len(issue_tp) + len(issue_fn)),
        "duplicate_precision": _metric("duplicate_precision", len(duplicate_tp), len(duplicate_tp) + len(duplicate_fp)),
        "duplicate_recall": _metric("duplicate_recall", len(duplicate_tp), len(duplicate_tp) + len(duplicate_fn)),
        "scope_accuracy": _metric("scope_accuracy", len(scope_ok), len(scope_ok) + len(scope_bad)),
        "posting_accuracy": _metric("posting_accuracy", len(posting_ok), len(posting_ok) + len(posting_bad)),
    }
    cost_locked = False
    pair_runs: dict[tuple[str, int, int, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    token_fields = (
        "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"
    )
    for run in run_by_id.values():
        if run["status"] == "not_invoked_setup_failure":
            continue
        key = (
            run["case_id"], run["repetition_index"], run["seed"],
            run["schedule_sha256"],
        )
        if run["arm"] in pair_runs[key]:
            raise BenchmarkError("duplicate cost arm for paired repetition")
        pair_runs[key][run["arm"]] = run
        if run["token_telemetry_status"] != "available":
            cost_locked = True
        elif any(field not in run for field in token_fields):
            raise BenchmarkError("available token telemetry is incomplete")
    valid_cost_pairs = []
    for pair in pair_runs.values():
        if set(pair) != {"baseline", "candidate"}:
            cost_locked = True
            continue
        baseline, candidate = pair["baseline"], pair["candidate"]
        for field in (
            "diff_sha256", "context_sha256", "seed", "schedule_sha256",
            "chunk_budget",
        ):
            if baseline[field] != candidate[field]:
                raise BenchmarkError("paired cost input identity mismatch")
        valid_cost_pairs.append({
            "baseline": _cost(baseline), "candidate": _cost(candidate)
        })
    baseline_cost = sum(value["baseline"] for value in valid_cost_pairs)
    candidate_cost = sum(value["candidate"] for value in valid_cost_pairs)
    if not valid_cost_pairs or baseline_cost == 0:
        cost_locked = True
    ratio = candidate_cost / baseline_cost if not cost_locked else 0.0
    metric_values["cost_regression"] = {"numerator": candidate_cost, "denominator": baseline_cost, "point_estimate": ratio, "wilson_95_lower_bound": 0.0, "threshold": 1.10, "passed": not cost_locked and ratio <= 1.10, "required_sample_shortfall": 0 if not cost_locked else 1}
    if cost_locked: invalid_reasons.add("cost_locked")
    if any(not value["passed"] for name, value in metric_values.items() if name != "cost_regression"):
        invalid_reasons.add("insufficient_sample")
    if not metric_values["cost_regression"]["passed"] and not cost_locked:
        invalid_reasons.add("cost_regression")
    if invalid_reasons & {"not_invoked_setup_failure"}:
        invalid_reasons.add("quality_locked")
    scoreable_primary_cases = {
        case_id for case_id, run in primary.items()
        if run["status"] != "not_invoked_setup_failure"
    }
    pr_size_strata_covered = len({
        manifest_by_case[case_id][1]["pr_size_stratum"]
        for case_id in scoreable_primary_cases
    })
    partial_timeout_cases = len({
        case_id for case_id, run in primary.items()
        if run["status"] in {"partial", "timeout"}
    })
    if len(primary_predictions) < 100:
        invalid_reasons.add("finding_sample")
    if pr_size_strata_covered < 3:
        invalid_reasons.add("pr_size_strata")
    if partial_timeout_cases < 10:
        invalid_reasons.add("partial_timeout_coverage")
    if len(hard_negative_audit) < 30:
        invalid_reasons.add("hard_negative_sample")
    primary_commitment = {
        case: {
            "run": run,
            "prediction_sha256": sorted(
                sha256_file(path)
                for path in prediction_paths_by_run.get(run["run_id"], [])
            ),
        }
        for case, run in sorted(primary.items())
    }
    paired_commitment = [
        run for run in sorted(
            run_by_id.values(), key=lambda item: item["run_id"]
        )
    ]
    audit = {"schema_version": "review-pipeline-private-score-audit-v1", "issue_tp": sorted(issue_tp), "issue_fp": sorted(issue_fp), "issue_fn": sorted(issue_fn), "duplicate_tp": sorted(duplicate_tp), "duplicate_fp": sorted(duplicate_fp), "duplicate_fn": sorted(duplicate_fn), "scope_match": sorted(scope_ok), "scope_mismatch": sorted(scope_bad), "posting_match": sorted(posting_ok), "posting_mismatch": sorted(posting_bad), "duplicate_predictions": sorted(duplicate_audit, key=lambda item: item["id"]), "hard_negative_pairs": sorted(hard_negative_audit), "true_paraphrase_negative_pairs": sorted(paraphrase_negative_audit)}
    private_file(workspace / "score-audit.json", canonical_json(audit) + b"\n")
    report = {"schema_version": "review-pipeline-benchmark-report-v1", "threshold_schema_version": "review-pipeline-thresholds-v1", "generated_at": generated_at, "valid_until": valid_until, "implementation_commit_sha": implementation_commit_sha, "identity": identity, "corpus_manifest_sha256": sha256_bytes(canonical_json({case: sha256_file(path) for case, (path, _) in sorted(manifest_by_case.items())})), "adjudication_commitment_sha256": sha256_bytes(canonical_json({case: sha256_bytes(canonical_json(answer)) for case, answer in sorted(answer_by_case.items())})), "primary_run_selection_sha256": sha256_bytes(canonical_json(primary_commitment)), "paired_schedule_sha256": sha256_bytes(canonical_json(paired_commitment)), "scorer_sha256": sha256_file(Path(__file__)), "schema_sha256": sha256_file(ROOT / "benchmarks/review_pipeline/schema/benchmark-report.schema.json"), "cost_model_version": "ntcu-v1", "metrics": metric_values, "finding_count": len(primary_predictions), "issue_count": sum(len(answer["issues"]) for answer in answer_by_case.values()), "rollout_evidence": {"case_count": len(primary), "pr_size_strata_covered": pr_size_strata_covered, "partial_timeout_cases": partial_timeout_cases, "hard_negative_pairs": len(hard_negative_audit)}, "can_enforce": not invalid_reasons, "failure_reasons": sorted(invalid_reasons)}
    validate_schema("benchmark-report", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifests", type=Path, required=True); parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True); parser.add_argument("--adjudication", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True); parser.add_argument("--implementation-commit-sha", required=True)
    parser.add_argument("--generated-at", required=True); parser.add_argument("--valid-until", required=True); parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = score(args.manifests, args.predictions, args.runs, args.adjudication, args.workspace, implementation_commit_sha=args.implementation_commit_sha, generated_at=args.generated_at, valid_until=args.valid_until)
        if args.report:
            if args.report.exists(): raise BenchmarkError("sanitized report destination already exists")
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_bytes(canonical_json(report))
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    except (BenchmarkError, OSError) as exc:
        print(f"score rejected: {exc}", file=sys.stderr); return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
