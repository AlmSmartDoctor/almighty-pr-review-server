#!/usr/bin/env python3
"""Run a deterministic blind offline fixture; live model execution is fail-closed.

No adjudication path is accepted or inspected by this command.  The fixture is an
injected prediction schedule, not a model transcript or label source.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_benchmark_common import (  # noqa: E402
    BenchmarkError,
    ROOT,
    canonical_json,
    load_json,
    private_directory,
    private_file,
    sha256_bytes,
    sha256_file,
    strict_json_loads,
    validate_schema,
)

HASH = re.compile(r"^[a-f0-9]{64}$")
BLIND_TERMS = re.compile(r'"(?:adjudication|answer|labels?|expected_defect|known_clean_ranges|issues?)"', re.IGNORECASE)
PREDICTION_FIELDS = {
    "prediction_id", "file", "line", "category", "normalized_claim_tokens",
    "production_claim_key", "emission_index", "scope_status", "posting_status",
    "duplicate_relation", "source_chunk_index",
}
IDENTITY_DEFAULTS = {
    "vendor": "offline-fixture", "model": "none", "effort": "none", "cli_version": "offline-fixture-v1",
}


def _digest(name: str) -> str:
    return sha256_bytes(name.encode("utf-8"))


def _load_blind_fixture(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise BenchmarkError("offline fixture must be a regular non-symlink file")
    if path.stat().st_size > 2_000_000:
        raise BenchmarkError("offline fixture exceeds byte cap")
    raw = path.read_text(encoding="utf-8")
    # Refuse label-bearing documents before parsing them, preserving blind execution.
    if BLIND_TERMS.search(raw):
        raise BenchmarkError("offline fixture contains prohibited adjudication or label field")
    try:
        fixture = strict_json_loads(raw)
    except BenchmarkError as exc:
        raise BenchmarkError("invalid offline fixture JSON") from exc
    allowed = {"schema_version", "identity", "schedule"}
    if not isinstance(fixture, dict) or set(fixture) - allowed or fixture.get("schema_version") != "review-benchmark-offline-fixture-v1":
        raise BenchmarkError("invalid offline fixture contract")
    if not isinstance(fixture.get("schedule"), list) or not fixture["schedule"]:
        raise BenchmarkError("offline fixture requires a non-empty schedule")
    if not isinstance(fixture.get("identity", {}), dict):
        raise BenchmarkError("offline fixture identity must be an object")
    return fixture


def _identity(raw: dict[str, Any]) -> dict[str, str]:
    allowed = {"vendor", "model", "effort", "cli_version", "protocol_sha256", "prompt_sha256", "chunker_sha256", "adapter_sha256", "event_schema_sha256", "chunk_budget"}
    if set(raw) - allowed:
        raise BenchmarkError("offline fixture identity has unknown field")
    identity = dict(IDENTITY_DEFAULTS)
    identity.update(raw)
    for name in ("vendor", "model", "effort", "cli_version"):
        if not isinstance(identity[name], str) or not identity[name]:
            raise BenchmarkError(f"invalid fixture identity {name}")
    chunk_budget = identity.get("chunk_budget", 100_000)
    if not isinstance(chunk_budget, int) or isinstance(chunk_budget, bool) or chunk_budget < 1:
        raise BenchmarkError("invalid fixture identity chunk_budget")
    identity["chunk_budget"] = chunk_budget
    for name in ("protocol_sha256", "prompt_sha256", "chunker_sha256", "adapter_sha256", "event_schema_sha256"):
        default_value = (
            sha256_file(ROOT / "server/review/diff_filter.py")
            if name == "chunker_sha256"
            else _digest(f"offline-fixture-v1:{name}")
        )
        value = identity.get(name, default_value)
        if not isinstance(value, str) or not HASH.fullmatch(value):
            raise BenchmarkError(f"invalid fixture identity hash {name}")
        identity[name] = value
    return identity


def _schedule_entry(entry: Any, case_id: str) -> dict[str, Any]:
    required = {"case_id", "arm", "repetition_index", "seed", "predictions"}
    allowed = required | {"status", "duration_ms", "tool_calls"}
    if not isinstance(entry, dict) or set(entry) - allowed or not required <= set(entry):
        raise BenchmarkError("offline schedule entry has invalid fields")
    if entry["case_id"] != case_id or entry["arm"] not in {"baseline", "candidate"}:
        raise BenchmarkError("offline schedule case or arm is invalid")
    if not isinstance(entry["repetition_index"], int) or entry["repetition_index"] < 0:
        raise BenchmarkError("offline schedule repetition index is invalid")
    if not isinstance(entry["seed"], int) or not 0 <= entry["seed"] <= 2147483647:
        raise BenchmarkError("offline schedule seed is invalid")
    if not isinstance(entry["predictions"], list) or not entry["predictions"]:
        raise BenchmarkError("offline schedule must provide injected predictions")
    entry.setdefault("status", "completed")
    entry.setdefault("duration_ms", 0)
    entry.setdefault("tool_calls", 0)
    if entry["status"] not in {"completed", "partial", "timeout", "failed", "not_invoked_setup_failure"}:
        raise BenchmarkError("offline schedule status is invalid")
    if not isinstance(entry["duration_ms"], int) or not 0 <= entry["duration_ms"] <= 86400000:
        raise BenchmarkError("offline schedule duration is invalid")
    if not isinstance(entry["tool_calls"], int) or not 0 <= entry["tool_calls"] <= 1000000:
        raise BenchmarkError("offline schedule tool call count is invalid")
    for prediction in entry["predictions"]:
        if not isinstance(prediction, dict) or set(prediction) - PREDICTION_FIELDS:
            raise BenchmarkError("offline prediction has prohibited raw output field")
    return entry


def run(manifest_path: Path, fixture_path: Path, workspace: Path, repetitions: int) -> dict[str, Any]:
    if manifest_path.is_symlink():
        raise BenchmarkError("manifest root symlink is forbidden")
    manifest_path = manifest_path.resolve(strict=True)
    manifest = load_json(manifest_path)
    validate_schema("manifest", manifest)
    if workspace.is_symlink():
        raise BenchmarkError("workspace symlink is forbidden")
    workspace = workspace.resolve(strict=True)
    mode = stat.S_IMODE(workspace.stat().st_mode)
    if not workspace.is_dir() or mode != 0o700:
        raise BenchmarkError("workspace must be an existing 0700 private directory")
    predictions_dir, runs_dir = workspace / "predictions", workspace / "runs"
    if predictions_dir.exists() or runs_dir.exists():
        raise BenchmarkError("workspace already contains run artifacts")

    fixture = _load_blind_fixture(fixture_path)
    identity = _identity(fixture.get("identity", {}))
    schedule = [_schedule_entry(item, manifest["case_id"]) for item in fixture["schedule"]]
    pairs: dict[tuple[int, int], set[str]] = {}
    for entry in schedule:
        pairs.setdefault((entry["repetition_index"], entry["seed"]), set()).add(entry["arm"])
    if ({repetition for repetition, _ in pairs} != set(range(repetitions)) or len(pairs) != repetitions
            or any(arms != {"baseline", "candidate"} for arms in pairs.values())):
        raise BenchmarkError("offline schedule must contain exactly paired baseline/candidate entries for every repetition")

    schedule_hash = sha256_bytes(canonical_json(schedule))
    primary_selection_hash = sha256_bytes(canonical_json([item for item in schedule if item["repetition_index"] == 0]))
    manifest_hash = sha256_file(manifest_path)
    input_path = manifest_path.parent / manifest["model_visible_input"]["path"]
    if not input_path.is_file() or input_path.is_symlink():
        raise BenchmarkError("model-visible input is missing or unsafe")
    diff_hash = sha256_file(input_path)
    if diff_hash != manifest["model_visible_input"]["sha256"]:
        raise BenchmarkError("model-visible input hash mismatch")

    private_directory(predictions_dir)
    private_directory(runs_dir)
    run_count = prediction_count = 0
    try:
        for entry in schedule:
            run_id = f"run-{manifest['case_id']}-{entry['arm']}-{entry['repetition_index']}"
            artifact_paths: list[Path] = []
            for index, raw_prediction in enumerate(entry["predictions"]):
                prediction = dict(raw_prediction)
                prediction.setdefault("prediction_id", f"prediction-{entry['arm']}-{entry['repetition_index']}-{index}")
                prediction.setdefault("emission_index", index)
                prediction.setdefault(
                    "production_claim_key",
                    " ".join(prediction.get("normalized_claim_tokens", [])),
                )
                prediction.update({
                    "schema_version": "review-pipeline-prediction-v1", "case_id": manifest["case_id"], "run_id": run_id,
                    "claim_normalization_version": manifest["claim_normalization_version"],
                    "claim_tokenizer_sha256": manifest["claim_tokenizer_sha256"],
                })
                validate_schema("prediction", prediction)
                artifact = predictions_dir / f"{run_id}-{prediction['prediction_id']}.json"
                private_file(artifact, canonical_json(prediction) + b"\n")
                artifact_paths.append(artifact)
                prediction_count += 1
            artifact_paths.sort(key=lambda item: item.name)
            artifact_hash = sha256_bytes(b"".join(
                len(item.name.encode("utf-8")).to_bytes(4, "big")
                + item.name.encode("utf-8")
                + bytes.fromhex(sha256_file(item))
                for item in artifact_paths
            ))
            run_result = {
                "schema_version": "review-pipeline-run-result-v1", "run_id": run_id, "case_id": manifest["case_id"],
                "manifest_sha256": manifest_hash, "arm": entry["arm"],
                "primary_repetition_index": 0,
                "repetition_index": entry["repetition_index"],
                "chunk_budget": identity["chunk_budget"],
                "seed": entry["seed"], "schedule_sha256": schedule_hash, **identity,
                "protocol_sha256": identity["protocol_sha256"], "prompt_sha256": identity["prompt_sha256"],
                "diff_sha256": diff_hash, "context_sha256": _digest("empty-context-v1"),
                "chunker_sha256": identity["chunker_sha256"], "adapter_sha256": identity["adapter_sha256"],
                "event_schema_sha256": identity["event_schema_sha256"],
                "prediction_artifact_path": f"benchmarks/review_pipeline/private/predictions/{artifact_paths[0].name}",
                "prediction_artifact_sha256": artifact_hash,
                "coverage_evidence_sha256": sha256_bytes(canonical_json({
                    "case_id": entry["case_id"], "arm": entry["arm"],
                    "repetition_index": entry["repetition_index"],
                    "seed": entry["seed"], "status": entry["status"],
                    "schedule_sha256": schedule_hash,
                })),
                "status": entry["status"], "duration_ms": entry["duration_ms"],
                "tool_calls": entry["tool_calls"], "token_telemetry_status": "unavailable",
            }
            validate_schema("run-result", run_result)
            private_file(runs_dir / f"{run_id}.json", canonical_json(run_result) + b"\n")
            run_count += 1
    except Exception:
        # Do not retain partial blind artifacts after a malformed fixture fails.
        for directory in (predictions_dir, runs_dir):
            for item in directory.glob("**/*"):
                if item.is_file():
                    item.unlink()
            directory.rmdir()
        raise
    return {"case_id": manifest["case_id"], "runs": run_count, "predictions": prediction_count,
            "schedule_sha256": schedule_hash, "primary_run_selection_sha256": primary_selection_hash,
            "fixture_sha256": sha256_file(fixture_path), "external_model_invoked": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--mode", choices=("offline-fixture", "live"), default="offline-fixture")
    args = parser.parse_args()
    if args.mode == "live":
        print("live runner is not authorized in this offline/local-only MVP", file=sys.stderr)
        return 2
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    try:
        print(json.dumps(run(args.manifest, args.fixture, args.workspace, args.repetitions), sort_keys=True))
    except (BenchmarkError, OSError) as exc:
        print(f"run rejected: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
