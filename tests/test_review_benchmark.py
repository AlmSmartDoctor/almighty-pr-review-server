import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from server.review.benchmark_attestation import (
    AttestationReason,
    BenchmarkIdentity,
    BenchmarkRuntimeIdentity,
    resolve_benchmark_attestation,
)
from server.review.finding_policy import resolve_policy_decision, resolve_policy_snapshot
from scripts.review_benchmark_common import (
    BenchmarkError,
    bernoulli_metric,
    strict_json_loads,
    validate_schema,
)
from server.review.pipeline_contracts import REVIEW_CHUNKER_VERSION
from server.review.rollout import evaluate_scope_dedupe_rollout


def test_benchmark_json_rejects_duplicate_keys():
    try:
        strict_json_loads('{"input_tokens":100,"input_tokens":1}')
    except BenchmarkError as exc:
        assert "duplicate JSON key" in str(exc)
    else:
        raise AssertionError("duplicate JSON key was accepted")


def test_synthetic_review_pipeline_benchmark_is_offline_and_green():
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, str(root / "scripts/review-pipeline-benchmark.py")],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["external_model_invoked"] is False
    assert report["labels_exposed_to_model"] is False
    assert report["metrics"]["scope_accuracy"] == 1.0
    assert report["metrics"]["duplicate_pair_precision"] == 1.0
    assert report["metrics"]["duplicate_pair_recall"] == 1.0
    assert report["rollout"]["can_enforce"] is False
    assert "insufficient_sample" in report["rollout"]["reasons"]


def test_rollout_gate_requires_precision_recall_and_sample_size():
    green = {
        "finding_count": 100,
        "scope_accuracy": 1.0,
        "enforce_posting_accuracy": 1.0,
        "duplicate_pair_precision": 1.0,
        "duplicate_pair_recall": 0.99,
        "issue_precision": 1.0,
        "issue_recall": 0.99,
        "confidence_95_lower_bound": 0.995,
        "pr_size_strata_covered": 3,
        "partial_timeout_cases": 10,
        "cost_regression_ratio": 1.0,
    }
    assert evaluate_scope_dedupe_rollout(green).can_enforce is True

    unsafe = dict(green, duplicate_pair_precision=0.99)
    decision = evaluate_scope_dedupe_rollout(unsafe)
    assert decision.can_enforce is False
    assert decision.reasons == ("duplicate_precision",)


def _schema_validate(schema, value, root=None):
    """Small deterministic validator for the schema keywords exercised by fixtures."""
    root = root or schema
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return _schema_validate(target, value, root)
    if "const" in schema:
        assert value == schema["const"]
    if "enum" in schema:
        assert value in schema["enum"]
    if "oneOf" in schema:
        matches = 0
        for option in schema["oneOf"]:
            try:
                _schema_validate(option, value, root)
            except AssertionError:
                continue
            matches += 1
        assert matches == 1
    expected_type = schema.get("type")
    if expected_type == "object" or any(key in schema for key in ("required", "properties", "additionalProperties")):
        assert isinstance(value, dict)
        for key in schema.get("required", []):
            assert key in value
        if schema.get("additionalProperties") is False:
            assert set(value) <= set(schema.get("properties", {}))
        for key, child in schema.get("properties", {}).items():
            if key in value:
                _schema_validate(child, value[key], root)
    elif expected_type == "array":
        assert isinstance(value, list)
        assert len(value) >= schema.get("minItems", 0)
        if schema.get("uniqueItems"):
            assert len({json.dumps(item, sort_keys=True) for item in value}) == len(value)
        if "items" in schema:
            for item in value:
                _schema_validate(schema["items"], item, root)
    elif expected_type == "string":
        assert isinstance(value, str)
        assert len(value) >= schema.get("minLength", 0)
        assert len(value) <= schema.get("maxLength", float("inf"))
        if "pattern" in schema:
            assert re.search(schema["pattern"], value) is not None
    elif expected_type == "integer":
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value >= schema.get("minimum", value)
        assert value <= schema.get("maximum", value)
    elif expected_type == "number":
        assert isinstance(value, (int, float)) and not isinstance(value, bool)
        assert value >= schema.get("minimum", value)
    elif expected_type == "boolean":
        assert isinstance(value, bool)


def _task_31_fixtures():
    digest = "a" * 64
    tokenizer = "9787d268171dfc88884d9961c1ae8608b111eda1eda892656a4db17622937458"
    ownership_hash = hashlib.sha256(
        (Path(__file__).parents[1] / "server/review/diff_filter.py").read_bytes()
    ).hexdigest()
    manifest = {
        "schema_version": "review-pipeline-manifest-v1", "case_id": "case-001",
        "source": {"source_id": "synthetic-case-001", "source_type": "synthetic", "source_url": "https://example.invalid/case-001", "immutable_revision_sha": "b" * 40, "license_spdx": "MIT", "redistribution_permitted": True},
        "patch_sha256": digest, "content_sha256": digest, "pr_size_stratum": "small",
        "model_visible_input": {"path": "inputs/case-001.diff", "sha256": digest},
        "provenance_approval_status": "approved", "contains_proprietary_code": False,
        "contains_jira_context": False, "contains_database_context": False,
        "contains_private_context": False, "claim_normalization_version": "claim-normalization-v1",
        "claim_tokenizer_sha256": tokenizer,
    }
    location = {"file": "src/parser.py", "line_start": 12, "line_end": 14}
    adjudication = {
        "schema_version": "review-pipeline-adjudication-v1", "case_id": "case-001", "manifest_sha256": digest,
        "claim_normalization_version": "claim-normalization-v1", "claim_tokenizer_sha256": tokenizer,
        "issues": [
            {"issue_id": "issue-001", "allowed_locations": [location], "accepted_categories": ["bug"], "canonical_claim_rubric": {"allowed_token_sequences": [["unchecked", "value"]], "evidence_tokens": ["value"]}},
            {"issue_id": "issue-002", "allowed_locations": [{"file": "src/parser.py", "line_start": 30, "line_end": 31}], "accepted_categories": ["bug"], "canonical_claim_rubric": {"allowed_token_sequences": [["missing", "guard"]], "evidence_tokens": ["guard"]}},
        ],
        "known_clean_ranges": [{"file": "src/parser.py", "line_start": 50, "line_end": 52}],
        "issue_pairs": [
            {"first_prediction_id": "prediction-001", "second_prediction_id": "prediction-002", "label": "same_issue_duplicate"},
            {"first_prediction_id": "prediction-001", "second_prediction_id": "prediction-003", "label": "distinct_issue_hard_negative"},
        ],
        "prediction_issue_resolutions": [],
        "adjudicator_verdicts": [
            {"adjudicator_id": "adj-abc123", "independent_verdict": "accept", "date": "2026-07-23", "disagreement_status": "none"},
            {"adjudicator_id": "adj-def456", "independent_verdict": "accept", "date": "2026-07-23", "disagreement_status": "none"},
        ],
        "resolution_status": "unanimous",
        "oracle_contract": {
            "ownership_function_version": "diff-ownership-v1",
            "ownership_function_sha256": ownership_hash,
            "chunker_version": REVIEW_CHUNKER_VERSION,
            "chunker_sha256": ownership_hash,
        },
    }
    prediction = {
        "schema_version": "review-pipeline-prediction-v1", "prediction_id": "prediction-001", "case_id": "case-001", "run_id": "run-001", "file": "src/parser.py", "line": 12, "category": "bug",
        "claim_normalization_version": "claim-normalization-v1", "claim_tokenizer_sha256": tokenizer,
        "normalized_claim_tokens": ["unchecked", "value"], "production_claim_key": "unchecked value", "emission_index": 0, "scope_status": "owned", "posting_status": "post", "duplicate_relation": "proposal_positive", "source_chunk_index": 0,
    }
    run = {
        "schema_version": "review-pipeline-run-result-v1", "run_id": "run-001", "case_id": "case-001", "manifest_sha256": digest, "arm": "candidate", "primary_repetition_index": 0, "repetition_index": 0, "chunk_budget": 100000, "seed": 7, "schedule_sha256": digest, "vendor": "offline-fixture", "model": "none", "effort": "none", "protocol_sha256": digest, "prompt_sha256": digest, "diff_sha256": digest, "context_sha256": digest, "chunker_sha256": ownership_hash, "adapter_sha256": digest, "cli_version": "offline", "event_schema_sha256": digest, "prediction_artifact_path": "benchmarks/review_pipeline/private/predictions/run-001.json", "prediction_artifact_sha256": digest, "coverage_evidence_sha256": digest, "status": "completed", "duration_ms": 0, "tool_calls": 0, "token_telemetry_status": "unavailable",
    }
    metric = {"numerator": 0, "denominator": 0, "point_estimate": 0, "wilson_95_lower_bound": 0, "threshold": 0, "passed": False, "required_sample_shortfall": 1}
    report = {
        "schema_version": "review-pipeline-benchmark-report-v1", "threshold_schema_version": "review-pipeline-thresholds-v1", "generated_at": "2026-07-23T00:00:00Z", "valid_until": "2026-08-23T00:00:00Z", "implementation_commit_sha": "c" * 40,
        "identity": {"vendor": "offline-fixture", "model": "none", "effort": "none", "prompt_sha256": digest, "protocol_sha256": digest, "chunker_sha256": digest, "chunk_budget": 100000, "adapter_sha256": digest, "cli_version": "offline", "event_schema_sha256": digest},
        "corpus_manifest_sha256": digest, "adjudication_commitment_sha256": digest, "primary_run_selection_sha256": digest, "paired_schedule_sha256": digest, "scorer_sha256": digest, "schema_sha256": hashlib.sha256((Path(__file__).parents[1] / "benchmarks/review_pipeline/schema/benchmark-report.schema.json").read_bytes()).hexdigest(), "cost_model_version": "ntcu-v1", "metrics": {name: dict(metric) for name in ("issue_precision", "issue_recall", "duplicate_precision", "duplicate_recall", "scope_accuracy", "posting_accuracy", "cost_regression")}, "finding_count": 0, "issue_count": 2, "rollout_evidence": {"case_count": 1, "pr_size_strata_covered": 1, "partial_timeout_cases": 0, "hard_negative_pairs": 0}, "can_enforce": False, "failure_reasons": ["insufficient_sample"],
    }
    return manifest, adjudication, prediction, run, report


def test_task_31_offline_schema_fixtures_cover_provenance_labels_and_contracts():
    root = Path(__file__).resolve().parents[1]
    schemas = {path.stem.replace(".schema", ""): json.loads(path.read_text()) for path in (root / "benchmarks/review_pipeline/schema").glob("*.json")}
    manifest, adjudication, prediction, run, report = _task_31_fixtures()
    for schema, fixture in zip(("manifest", "adjudication", "prediction", "run-result", "benchmark-report"), (manifest, adjudication, prediction, run, report), strict=True):
        _schema_validate(schemas[schema], fixture)

    assert all(manifest[flag] is False for flag in ("contains_proprietary_code", "contains_jira_context", "contains_database_context", "contains_private_context"))
    assert set(adjudication["issues"][0]["canonical_claim_rubric"]["allowed_token_sequences"][0]) == {"unchecked", "value"}
    assert {pair["label"] for pair in adjudication["issue_pairs"]} == {"same_issue_duplicate", "distinct_issue_hard_negative"}
    assert adjudication["adjudicator_verdicts"][0]["adjudicator_id"].startswith("adj-")
    assert len(adjudication["adjudicator_verdicts"][0]["date"]) == 10
    assert prediction["normalized_claim_tokens"] == adjudication["issues"][0]["canonical_claim_rubric"]["allowed_token_sequences"][0]
    assert "issues" not in schemas["manifest"]["properties"]
    assert "expected_defect" not in schemas["manifest"]["properties"]

    bad_manifest = dict(manifest, expected_defect="label leak")
    for invalid_manifest, message in ((bad_manifest, "unknown label field"), (dict(manifest, contains_proprietary_code=True), "proprietary flag")):
        try:
            _schema_validate(schemas["manifest"], invalid_manifest)
        except AssertionError:
            pass
        else:
            raise AssertionError(f"strict manifest schema accepted {message}")
    bad_source = dict(manifest["source"], owner_email="private@example.invalid")
    try:
        _schema_validate(schemas["manifest"], dict(manifest, source=bad_source))
    except AssertionError:
        pass
    else:
        raise AssertionError("strict provenance schema accepted an unknown sensitive field")


def _local_bundle(tmp_path, *, flag=None, extra_field=None):
    bundle = tmp_path / "bundle"
    inputs = bundle / "inputs"
    inputs.mkdir(parents=True)
    content = b"diff --git a/src/parser.py b/src/parser.py\n+guard(value)\n"
    (inputs / "case-001.diff").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    manifest, _, _, _, _ = _task_31_fixtures()
    manifest["patch_sha256"] = digest
    manifest["content_sha256"] = digest
    manifest["model_visible_input"]["sha256"] = digest
    if flag:
        manifest[flag] = True
    if extra_field:
        manifest[extra_field] = "label leak"
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle, manifest


def _bundle_hash(bundle):
    digest = hashlib.sha256()
    for path in sorted(bundle.rglob("*")):
        if path.is_file():
            relative = path.relative_to(bundle).as_posix().encode()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(bytes.fromhex(hashlib.sha256(path.read_bytes()).hexdigest()))
    return digest.hexdigest()


def _benchmark_command(root, script, *args):
    return subprocess.run(
        [sys.executable, str(root / "scripts" / script), *map(str, args)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_task_32_local_collect_rejects_labels_proprietary_and_symlinks(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for name, mutate in (("label", {"extra_field": "expected_defect"}), ("proprietary", {"flag": "contains_proprietary_code"})):
        bundle, _ = _local_bundle(tmp_path / name, **mutate)
        result = _benchmark_command(root, "review-benchmark-collect.py", "--bundle", bundle, "--workspace", tmp_path / f"{name}-workspace", "--expected-bundle-sha256", _bundle_hash(bundle))
        assert result.returncode == 2, result.stdout

    bundle, _ = _local_bundle(tmp_path / "symlink")
    os.symlink("case-001.diff", bundle / "inputs" / "linked.diff")
    result = _benchmark_command(root, "review-benchmark-collect.py", "--bundle", bundle, "--workspace", tmp_path / "symlink-workspace", "--expected-bundle-sha256", _bundle_hash(bundle))
    assert result.returncode == 2
    assert "symlink" in result.stderr


def test_task_32_collect_private_permissions_and_deterministic_blind_fixture(tmp_path):
    root = Path(__file__).resolve().parents[1]
    bundle, manifest = _local_bundle(tmp_path)
    workspace = tmp_path / "workspace"
    collected = _benchmark_command(root, "review-benchmark-collect.py", "--bundle", bundle, "--workspace", workspace, "--expected-bundle-sha256", _bundle_hash(bundle))
    assert collected.returncode == 0, collected.stderr
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in workspace.rglob("*") if path.is_file())

    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({
        "schema_version": "review-benchmark-offline-fixture-v1",
        "schedule": [
            {"case_id": manifest["case_id"], "arm": arm, "repetition_index": 0, "seed": 7, "predictions": [{"file": "src/parser.py", "line": 2, "category": "bug", "normalized_claim_tokens": ["guard"], "scope_status": "owned", "posting_status": "post", "duplicate_relation": "proposal_negative", "source_chunk_index": 0}]}
            for arm in ("baseline", "candidate")
        ],
    }), encoding="utf-8")
    first = _benchmark_command(root, "review-benchmark-run.py", "--manifest", workspace / "manifest.json", "--fixture", fixture, "--workspace", workspace)
    assert first.returncode == 0, first.stderr
    report = json.loads(first.stdout)
    assert report["external_model_invoked"] is False
    assert report["runs"] == 2
    assert stat.S_IMODE((workspace / "predictions").stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (workspace / "runs").glob("*.json"))

    copied_workspace = tmp_path / "workspace-2"
    shutil.copytree(workspace, copied_workspace)
    shutil.rmtree(copied_workspace / "predictions")
    shutil.rmtree(copied_workspace / "runs")
    os.chmod(copied_workspace, 0o700)
    second = _benchmark_command(root, "review-benchmark-run.py", "--manifest", copied_workspace / "manifest.json", "--fixture", fixture, "--workspace", copied_workspace)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["schedule_sha256"] == report["schedule_sha256"]
    live = _benchmark_command(root, "review-benchmark-run.py", "--mode", "live", "--manifest", workspace / "manifest.json", "--fixture", fixture, "--workspace", workspace)
    assert live.returncode == 2
    assert "not authorized" in live.stderr


def test_task_32_lint_rejects_shared_trees_and_prediction_label_leakage(tmp_path):
    root = Path(__file__).resolve().parents[1]
    bundle, manifest = _local_bundle(tmp_path)
    predictions = tmp_path / "predictions"
    answers = tmp_path / "answers"
    predictions.mkdir()
    answers.mkdir()
    _, adjudication, prediction, _, _ = _task_31_fixtures()
    adjudication["issue_pairs"] = []
    adjudication["manifest_sha256"] = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
    (answers / "answer.json").write_text(json.dumps(adjudication), encoding="utf-8")
    prediction["label"] = "should never be present"
    (predictions / "prediction.json").write_text(json.dumps(prediction), encoding="utf-8")
    leaked = _benchmark_command(root, "review-benchmark-lint.py", "--manifest", bundle / "manifest.json", "--predictions", predictions, "--adjudication", answers)
    assert leaked.returncode == 2

    prediction.pop("label")
    (predictions / "prediction.json").write_text(json.dumps(prediction), encoding="utf-8")
    valid = _benchmark_command(root, "review-benchmark-lint.py", "--manifest", bundle / "manifest.json", "--predictions", predictions, "--adjudication", answers)
    assert valid.returncode == 0, valid.stderr
    shared = _benchmark_command(root, "review-benchmark-lint.py", "--manifest", bundle / "manifest.json", "--predictions", bundle, "--adjudication", answers)
    assert shared.returncode == 2


def _score_roots(tmp_path, *, ambiguous=False, unavailable_tokens=False):
    manifests, predictions, runs, answers, workspace = [tmp_path / item for item in ("manifests", "predictions", "runs", "answers", "audit")]
    for directory in (manifests, predictions, runs, answers):
        directory.mkdir(parents=True)
    workspace.mkdir(mode=0o700)
    diff = b"diff --git a/src/parser.py b/src/parser.py\n--- a/src/parser.py\n+++ b/src/parser.py\n@@ -12 +12 @@\n-old\n+new\n"
    (manifests / "inputs").mkdir(); (manifests / "inputs/case-001.diff").write_bytes(diff)
    manifest, adjudication, prediction, run, _ = _task_31_fixtures()
    digest = hashlib.sha256(diff).hexdigest()
    manifest.update({"patch_sha256": digest, "content_sha256": digest})
    manifest["model_visible_input"]["sha256"] = digest
    (manifests / "manifest.json").write_text(json.dumps(manifest))
    manifest_hash = hashlib.sha256((manifests / "manifest.json").read_bytes()).hexdigest()
    adjudication["manifest_sha256"] = manifest_hash
    adjudication["issues"][1]["allowed_locations"] = [dict(adjudication["issues"][0]["allowed_locations"][0])]
    adjudication["issues"][1]["canonical_claim_rubric"]["allowed_token_sequences"] = [["guard", "missing"]]
    if ambiguous:
        adjudication["issues"][1]["canonical_claim_rubric"] = copy.deepcopy(adjudication["issues"][0]["canonical_claim_rubric"])
    else:
        adjudication["issue_pairs"] = [
            {"first_prediction_id": "prediction-0", "second_prediction_id": "prediction-1", "label": "same_issue_duplicate"},
            {"first_prediction_id": "prediction-0", "second_prediction_id": "prediction-2", "label": "distinct_issue_hard_negative"},
        ]
    (answers / "answer.json").write_text(json.dumps(adjudication))
    base = copy.deepcopy(run)
    base.update({"run_id": "run-baseline", "arm": "baseline", "manifest_sha256": manifest_hash, "schedule_sha256": "b" * 64, "diff_sha256": digest, "token_telemetry_status": "unavailable" if unavailable_tokens else "available", "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "prediction_artifact_sha256": "c" * 64})
    candidate = copy.deepcopy(base); candidate.update({"run_id": "run-candidate", "arm": "candidate", "input_tokens": 10})
    items = []
    for index, tokens in enumerate((["unchecked", "value"], ["unchecked", "value"], ["guard", "missing"])):
        item = copy.deepcopy(prediction)
        item.update({"prediction_id": f"prediction-{index}", "run_id": "run-candidate", "line": 12, "normalized_claim_tokens": tokens, "production_claim_key": " ".join(tokens), "emission_index": index, "scope_status": "owned", "posting_status": "post", "duplicate_relation": "proposal_positive" if index < 2 else "proposal_negative"})
        items.append(item)
        (predictions / f"{item['prediction_id']}.json").write_text(json.dumps(item))
    baseline_prediction = copy.deepcopy(prediction)
    baseline_prediction.update({
        "prediction_id": "prediction-baseline",
        "run_id": "run-baseline",
        "line": 12,
        "source_chunk_index": 0,
        "normalized_claim_tokens": ["unchecked", "value"],
        "production_claim_key": "unchecked value",
        "emission_index": 0,
        "scope_status": "owned",
        "posting_status": "post",
        "duplicate_relation": "proposal_negative",
    })
    (predictions / "prediction-baseline.json").write_text(
        json.dumps(baseline_prediction)
    )
    for value in (base, candidate):
        artifact_paths = sorted(
            path for path in predictions.glob("*.json")
            if json.loads(path.read_text())["run_id"] == value["run_id"]
        )
        value["prediction_artifact_path"] = (
            "benchmarks/review_pipeline/private/predictions/"
            + artifact_paths[0].name
        )
        value["prediction_artifact_sha256"] = hashlib.sha256(b"".join(
            len(path.name.encode()).to_bytes(4, "big")
            + path.name.encode()
            + bytes.fromhex(hashlib.sha256(path.read_bytes()).hexdigest())
            for path in artifact_paths
        )).hexdigest()
        coverage = {
            "case_id": value["case_id"], "arm": value["arm"],
            "repetition_index": value["repetition_index"],
            "seed": value["seed"], "status": value["status"],
            "schedule_sha256": value["schedule_sha256"],
        }
        value["coverage_evidence_sha256"] = hashlib.sha256(
            json.dumps(
                coverage, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        (runs / f"{value['run_id']}.json").write_text(json.dumps(value))
    return manifests, predictions, runs, answers, workspace


def test_task_32_score_offline_join_audit_wilson_and_cost_lock(tmp_path):
    root = Path(__file__).resolve().parents[1]
    manifests, predictions, runs, answers, workspace = _score_roots(tmp_path)
    result = _benchmark_command(root, "review-benchmark-score.py", "--manifests", manifests, "--predictions", predictions, "--runs", runs, "--adjudication", answers, "--workspace", workspace, "--implementation-commit-sha", "d" * 40, "--generated-at", "2026-07-23T00:00:00Z", "--valid-until", "2026-08-23T00:00:00Z")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["metrics"]["issue_precision"]["numerator"] == 2
    assert report["metrics"]["duplicate_precision"]["numerator"] == 1
    assert report["metrics"]["posting_accuracy"]["numerator"] == 2
    assert report["metrics"]["posting_accuracy"]["denominator"] == 3
    assert report["metrics"]["duplicate_precision"]["required_sample_shortfall"] > 0
    assert report["metrics"]["issue_precision"]["wilson_95_lower_bound"] < 1
    assert report["rollout_evidence"]["hard_negative_pairs"] == 1
    decision = evaluate_scope_dedupe_rollout(report)
    assert decision.can_enforce is False
    assert "duplicate_precision" in decision.reasons
    assert stat.S_IMODE((workspace / "score-audit.json").stat().st_mode) == 0o600
    assert "src/parser.py" not in result.stdout and "unchecked" not in result.stdout

    manifests, predictions, runs, answers, workspace = _score_roots(tmp_path / "locked", unavailable_tokens=True)
    locked = _benchmark_command(root, "review-benchmark-score.py", "--manifests", manifests, "--predictions", predictions, "--runs", runs, "--adjudication", answers, "--workspace", workspace, "--implementation-commit-sha", "d" * 40, "--generated-at", "2026-07-23T00:00:00Z", "--valid-until", "2026-08-23T00:00:00Z")
    assert locked.returncode == 0, locked.stderr
    assert "cost_locked" in json.loads(locked.stdout)["failure_reasons"]


def test_task_32_score_rejects_prediction_artifact_tampering(tmp_path):
    root = Path(__file__).resolve().parents[1]
    manifests, predictions, runs, answers, workspace = _score_roots(tmp_path)
    target = predictions / "prediction-0.json"
    changed = json.loads(target.read_text())
    changed["category"] = "security"
    target.write_text(json.dumps(changed))
    result = _benchmark_command(
        root, "review-benchmark-score.py", "--manifests", manifests,
        "--predictions", predictions, "--runs", runs,
        "--adjudication", answers, "--workspace", workspace,
        "--implementation-commit-sha", "d" * 40,
        "--generated-at", "2026-07-23T00:00:00Z",
        "--valid-until", "2026-08-23T00:00:00Z",
    )
    assert result.returncode == 2
    assert "artifact hash mismatch" in result.stderr


def test_task_32_score_rejects_ambiguous_claim_without_resolution(tmp_path):
    root = Path(__file__).resolve().parents[1]
    manifests, predictions, runs, answers, workspace = _score_roots(tmp_path, ambiguous=True)
    result = _benchmark_command(root, "review-benchmark-score.py", "--manifests", manifests, "--predictions", predictions, "--runs", runs, "--adjudication", answers, "--workspace", workspace, "--implementation-commit-sha", "d" * 40, "--generated-at", "2026-07-23T00:00:00Z", "--valid-until", "2026-08-23T00:00:00Z")
    assert result.returncode == 2
    assert "ambiguous" in result.stderr


def _attestation_report(tmp_path):
    _, _, _, _, report = _task_31_fixtures()
    report["metrics"] = {
        "issue_precision": bernoulli_metric(
            1000, 1000, point_threshold=0.995, lower_threshold=0.99
        ),
        "issue_recall": bernoulli_metric(
            1000, 1000, point_threshold=0.95, lower_threshold=0.90
        ),
        "duplicate_precision": bernoulli_metric(
            30, 30, point_threshold=1.0, lower_threshold=0.88,
            minimum_denominator=30,
        ),
        "duplicate_recall": bernoulli_metric(
            30, 30, point_threshold=0.95, lower_threshold=0.85,
            minimum_denominator=30,
        ),
        "scope_accuracy": bernoulli_metric(
            1000, 1000, point_threshold=0.995, lower_threshold=0.99
        ),
        "posting_accuracy": bernoulli_metric(
            1000, 1000, point_threshold=0.995, lower_threshold=0.99
        ),
        "cost_regression": {
            "numerator": 100, "denominator": 100, "point_estimate": 1.0,
            "wilson_95_lower_bound": 0.0, "threshold": 1.10,
            "passed": True, "required_sample_shortfall": 0,
        },
    }
    report.update({
        "generated_at": "2026-07-23T00:00:00Z",
        "valid_until": "2026-08-23T00:00:00Z",
        "implementation_commit_sha": "c" * 40,
        "finding_count": 1000,
        "issue_count": 1000,
        "rollout_evidence": {
            "case_count": 10,
            "pr_size_strata_covered": 3,
            "partial_timeout_cases": 10,
            "hard_negative_pairs": 30,
        },
        "can_enforce": True,
        "failure_reasons": [],
    })
    path = tmp_path / "attestation.json"
    path.write_bytes(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return path, report


def _attestation_identity(report):
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
        paired_schedule_sha256=report["paired_schedule_sha256"], scorer_sha256=report["scorer_sha256"],
        schema_sha256=report["schema_sha256"],
    )


def _runtime_identity(report):
    identity = report["identity"]
    return BenchmarkRuntimeIdentity(**{
        field: identity[field]
        for field in BenchmarkRuntimeIdentity.__dataclass_fields__
    })


def _write_attestation(path, report):
    path.write_bytes(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_benchmark_attestation_fails_closed_for_report_and_identity_mismatches(tmp_path, monkeypatch):
    path, report = _attestation_report(tmp_path)
    expected = _attestation_identity(report)
    digest = _write_attestation(path, report)
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "server.review.benchmark_attestation._clean_head",
        lambda root: ("c" * 40, None),
    )
    runtime = _runtime_identity(report)
    assert resolve_benchmark_attestation(
        report_path=path, expected_hash=digest, expected_identity=expected, now=now,
    ).reason is AttestationReason.RUNTIME_IDENTITY_MISSING
    mismatch = copy.deepcopy(runtime.__dict__)
    mismatch["model"] = "different"
    assert resolve_benchmark_attestation(
        report_path=path, expected_hash=digest, expected_identity=expected,
        runtime_identity=mismatch, now=now,
    ).reason is AttestationReason.RUNTIME_IDENTITY_MISMATCH
    valid = resolve_benchmark_attestation(
        report_path=path, expected_hash=digest, expected_identity=expected,
        runtime_identity=runtime, now=now,
    )
    assert valid.can_enforce and valid.reason is AttestationReason.VALID
    assert valid.report_hash == digest and valid.identity == expected

    forged = copy.deepcopy(report)
    forged["metrics"]["issue_precision"].update(
        numerator=0, denominator=0, point_estimate=1.0,
        wilson_95_lower_bound=1.0, passed=True,
        required_sample_shortfall=0,
    )
    forged_digest = _write_attestation(path, forged)
    assert resolve_benchmark_attestation(
        report_path=path, expected_hash=forged_digest,
        expected_identity=expected, now=now,
    ).reason is AttestationReason.REPORT_INVALID

    for mutate in (
        lambda value: value.update(finding_count=100),
        lambda value: value.update(issue_count=999999),
        lambda value: value["rollout_evidence"].update(
            pr_size_strata_covered=999
        ),
    ):
        forged = copy.deepcopy(report)
        mutate(forged)
        forged_digest = _write_attestation(path, forged)
        assert resolve_benchmark_attestation(
            report_path=path, expected_hash=forged_digest,
            expected_identity=expected, now=now,
        ).reason is AttestationReason.REPORT_INVALID

    assert resolve_benchmark_attestation(
        report_path=path, expected_hash="0" * 64, expected_identity=expected, now=now,
    ).reason is AttestationReason.REPORT_HASH_MISMATCH
    assert resolve_benchmark_attestation(
        report_path=path, expected_hash=digest, expected_identity=None, now=now,
    ).reason is AttestationReason.MISSING_EXPECTED_IDENTITY

    for field in BenchmarkIdentity.__dataclass_fields__:
        changed = dict(report)
        changed["identity"] = dict(report["identity"])
        if field in changed["identity"]:
            if field == "chunk_budget":
                changed["identity"][field] = 999
            else:
                changed["identity"][field] = (
                    "different" if not field.endswith("_sha256") else "d" * 64
                )
        elif field == "implementation_commit_sha":
            changed[field] = "d" * 40
        else:
            changed[field] = "d" * 64
        changed_digest = _write_attestation(path, changed)
        decision = resolve_benchmark_attestation(
            report_path=path, expected_hash=changed_digest, expected_identity=expected, now=now,
        )
        assert decision.can_enforce is False
        expected_reason = (
            AttestationReason.IMPLEMENTATION_COMMIT_MISMATCH
            if field == "implementation_commit_sha"
            else AttestationReason.IDENTITY_MISMATCH
        )
        assert decision.reason is expected_reason


def test_benchmark_attestation_rejects_schema_expiry_dirty_and_rollout_locks(tmp_path, monkeypatch):
    path, report = _attestation_report(tmp_path)
    expected = _attestation_identity(report)
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "server.review.benchmark_attestation._clean_head",
        lambda root: ("c" * 40, None),
    )
    cases = [
        (lambda value: value.pop("schema_sha256"), AttestationReason.REPORT_UNAVAILABLE),
        (lambda value: value.update(valid_until="2026-07-24T00:00:00Z"), AttestationReason.EXPIRED),
        (lambda value: value.update(can_enforce=False), AttestationReason.REPORT_INVALID),
        (lambda value: value.update(failure_reasons=["quality_locked"]), AttestationReason.REPORT_INVALID),
    ]
    for mutate, reason in cases:
        candidate = copy.deepcopy(report)
        mutate(candidate)
        digest = _write_attestation(path, candidate)
        decision = resolve_benchmark_attestation(
            report_path=path, expected_hash=digest, expected_identity=expected, now=now,
        )
        assert decision.can_enforce is False and decision.reason is reason
        assert decision.report_hash is None

    digest = _write_attestation(path, report)
    monkeypatch.setattr(
        "server.review.benchmark_attestation._clean_head",
        lambda root: (None, AttestationReason.IMPLEMENTATION_DIRTY),
    )
    assert resolve_benchmark_attestation(
        report_path=path, expected_hash=digest, expected_identity=expected, now=now,
    ).reason is AttestationReason.IMPLEMENTATION_DIRTY


def test_policy_enforcement_requires_exact_runtime_candidate_identity(monkeypatch):
    runtime = BenchmarkRuntimeIdentity(
        vendor="codex", model="gpt", effort="high",
        prompt_sha256="a" * 64, protocol_sha256="b" * 64,
        chunker_sha256="c" * 64, chunk_budget=100_000,
        adapter_sha256="d" * 64, cli_version="1.2.3",
        event_schema_sha256="e" * 64,
    )
    monkeypatch.setattr("server.config.REVIEW_POLICY_ENFORCEMENT_UNLOCKED", True)
    monkeypatch.setattr("server.config.REVIEW_SCOPE_KILL_SWITCH", False)
    monkeypatch.setattr("server.config.REVIEW_DEDUPE_KILL_SWITCH", False)

    def attestation(*, runtime_identity=None):
        valid = runtime_identity == runtime
        return type("Decision", (), {
            "can_enforce": valid,
            "report_hash": "f" * 64 if valid else None,
        })()

    monkeypatch.setattr(
        "server.review.finding_policy.resolve_benchmark_attestation", attestation
    )
    repo = {
        "full_name": "acme/api",
        "review_scope_guard_mode": "enforce",
        "review_dedupe_mode": "enforce",
    }
    assert resolve_policy_snapshot(repo).scope.effective_mode == "observe"
    mismatched = BenchmarkRuntimeIdentity(**{**runtime.__dict__, "model": "other"})
    assert resolve_policy_snapshot(
        repo, benchmark_runtime_identity=mismatched
    ).scope.effective_mode == "observe"
    exact = resolve_policy_snapshot(repo, benchmark_runtime_identity=runtime)
    assert exact.scope.effective_mode == exact.dedupe.effective_mode == "enforce"
    assert exact.benchmark_attestation_hash == "f" * 64


def test_unlock_never_bypasses_attestation_canary_or_kill_switch(monkeypatch):
    repo = {"full_name": "acme/api", "review_scope_guard_mode": None}
    monkeypatch.setattr("server.config.REVIEW_POLICY_ENFORCEMENT_UNLOCKED", True)
    monkeypatch.setattr("server.config.REVIEW_SCOPE_ENFORCE_REPOS", frozenset({"acme/api"}))
    monkeypatch.setattr("server.config.REVIEW_SCOPE_KILL_SWITCH", False)
    assert resolve_policy_decision(repo, policy="scope", default_mode="enforce").effective_mode == "observe"

    class Valid:
        can_enforce = True
    assert resolve_policy_decision(
        repo, policy="scope", default_mode="enforce", benchmark_attestation=Valid()
    ).effective_mode == "enforce"
    monkeypatch.setattr("server.config.REVIEW_SCOPE_KILL_SWITCH", True)
    assert resolve_policy_decision(
        repo, policy="scope", default_mode="enforce", benchmark_attestation=Valid()
    ).effective_mode == "observe"


def test_adjudication_requires_two_independent_completed_verdicts():
    _, adjudication, *_ = _task_31_fixtures()
    single = copy.deepcopy(adjudication)
    single["adjudicator_verdicts"] = single["adjudicator_verdicts"][:1]
    with pytest.raises(BenchmarkError):
        validate_schema("adjudication", single)

    unresolved = copy.deepcopy(adjudication)
    unresolved["resolution_status"] = "unresolved"
    with pytest.raises(BenchmarkError, match="unresolved"):
        validate_schema("adjudication", unresolved)

    resolved_without_record = copy.deepcopy(adjudication)
    resolved_without_record["resolution_status"] = "resolved"
    for verdict in resolved_without_record["adjudicator_verdicts"]:
        verdict["disagreement_status"] = "resolved"
    with pytest.raises(BenchmarkError):
        validate_schema("adjudication", resolved_without_record)


def test_task_31_private_benchmark_workspaces_are_ignored_without_staging():
    root = Path(__file__).resolve().parents[1]
    private_paths = (
        "benchmarks/review_pipeline/private/manifests/case-001.json",
        "benchmarks/review_pipeline/private/answers/case-001.json",
        "benchmarks/review_pipeline/private/predictions/run-001.json",
        "benchmarks/review_pipeline/private/runs/run-001.json",
        "benchmarks/review_pipeline/results/report.json",
    )
    for path in private_paths:
        completed = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", path],
            cwd=root,
            check=False,
        )
        assert completed.returncode == 0, path
