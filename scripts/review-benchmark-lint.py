#!/usr/bin/env python3
"""Validate separated local benchmark artifacts without joining labels to predictions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_benchmark_common import (  # noqa: E402
    BenchmarkError,
    assert_safe_tree,
    disjoint_paths,
    load_json,
    sha256_file,
    validate_schema,
)

FORBIDDEN_PREDICTION_KEYS = {"rationale", "stdout", "stderr", "raw_stdout", "raw_stderr", "label", "labels", "answer", "adjudication", "expected_defect"}


def _json_files(path: Path) -> list[Path]:
    if path.is_file():
        if path.is_symlink() or path.suffix != ".json":
            raise BenchmarkError(f"expected JSON file: {path}")
        return [path]
    files = assert_safe_tree(path)
    result = [item for item in files if item.suffix == ".json"]
    if not result:
        raise BenchmarkError(f"no JSON artifacts in {path}")
    if len(result) != len(files):
        raise BenchmarkError(f"non-JSON artifact in {path}")
    return result


def _validate_many(path: Path, schema: str) -> list[tuple[Path, dict]]:
    values: list[tuple[Path, dict]] = []
    for item in _json_files(path):
        value = load_json(item)
        validate_schema(schema, value)
        values.append((item, value))
    return values


def lint(manifest_path: Path, predictions_path: Path, adjudication_path: Path, runs_path: Path | None = None) -> dict:
    if manifest_path.is_symlink():
        raise BenchmarkError("manifest symlink is forbidden")
    manifest_path = manifest_path.resolve(strict=True)
    # Use containing roots, not individual files, so placing artifacts under one tree fails.
    roots = [manifest_path.parent, predictions_path, adjudication_path]
    if runs_path is not None:
        roots.append(runs_path)
    disjoint_paths(roots)

    assert_safe_tree(manifest_path.parent)
    manifest = load_json(manifest_path)
    validate_schema("manifest", manifest)
    if any(key in manifest for key in ("issues", "labels", "answer", "adjudication", "expected_defect", "known_clean_ranges")):
        raise BenchmarkError("model manifest contains label material")
    predictions = _validate_many(predictions_path, "prediction")
    answers = _validate_many(adjudication_path, "adjudication")
    runs = _validate_many(runs_path, "run-result") if runs_path is not None else []

    answer_by_case = {value["case_id"]: value for _, value in answers}
    if len(answer_by_case) != len(answers):
        raise BenchmarkError("duplicate adjudication case ID")
    for _, value in predictions:
        if set(value) & FORBIDDEN_PREDICTION_KEYS:
            raise BenchmarkError("prediction contains raw output or label material")
        if value["case_id"] != manifest["case_id"]:
            raise BenchmarkError("prediction case does not match manifest")
        if value["case_id"] not in answer_by_case:
            raise BenchmarkError("prediction has no separately supplied adjudication")
    manifest_hash = sha256_file(manifest_path)
    prediction_ids = {value["prediction_id"] for _, value in predictions}
    for _, answer in answers:
        if answer["case_id"] != manifest["case_id"] or answer["manifest_sha256"] != manifest_hash:
            raise BenchmarkError("adjudication does not bind to supplied manifest")
        referenced = {
            item
            for pair in answer["issue_pairs"]
            for item in (
                pair["first_prediction_id"], pair["second_prediction_id"]
            )
        }
        referenced.update(
            item["prediction_id"]
            for item in answer["prediction_issue_resolutions"]
        )
        if not referenced <= prediction_ids:
            raise BenchmarkError("adjudication references unknown prediction")
    for _, run in runs:
        if run["case_id"] != manifest["case_id"] or run["manifest_sha256"] != manifest_hash:
            raise BenchmarkError("run does not bind to supplied manifest")

    return {"manifest": manifest["case_id"], "predictions": len(predictions), "adjudications": len(answers), "runs": len(runs)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--adjudication", type=Path, required=True)
    parser.add_argument("--runs", type=Path)
    args = parser.parse_args()
    try:
        report = lint(args.manifest, args.predictions, args.adjudication, args.runs)
    except (BenchmarkError, OSError) as exc:
        print(f"lint rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
