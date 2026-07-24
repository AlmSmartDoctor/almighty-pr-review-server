#!/usr/bin/env python3
"""Copy one approved local public/synthetic benchmark bundle into a private workspace.

This MVP intentionally has no downloader, URL client, archive handling, or network path.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_benchmark_common import (  # noqa: E402
    BenchmarkError,
    assert_safe_tree,
    load_json,
    private_directory,
    private_file,
    sha256_file,
    tree_sha256,
    validate_schema,
)


def collect(bundle: Path, workspace: Path, expected_bundle_sha256: str, max_files: int, max_bytes: int) -> dict:
    if workspace.exists() or workspace.is_symlink():
        raise BenchmarkError("workspace must be new")
    if bundle.is_symlink():
        raise BenchmarkError("bundle root symlink is forbidden")
    bundle = bundle.resolve(strict=True)
    files = assert_safe_tree(bundle, max_files=max_files, max_bytes=max_bytes)
    actual_bundle_sha256 = tree_sha256(bundle, files)
    if actual_bundle_sha256 != expected_bundle_sha256:
        raise BenchmarkError("approved immutable bundle hash mismatch")

    manifest_path = bundle / "manifest.json"
    if manifest_path not in files:
        raise BenchmarkError("bundle must contain manifest.json")
    manifest = load_json(manifest_path)
    validate_schema("manifest", manifest)
    input_relative = Path(manifest["model_visible_input"]["path"])
    input_path = bundle / input_relative
    if input_path not in files or not input_relative.parts or input_relative.parts[0] != "inputs":
        raise BenchmarkError("model-visible input is missing or outside inputs directory")
    # A minimal approved bundle is deliberately closed: any extra file could carry labels.
    allowed = {manifest_path, input_path}
    if set(files) != allowed:
        raise BenchmarkError("bundle contains files outside manifest and model-visible input")
    content_hash = sha256_file(input_path)
    if manifest["model_visible_input"]["sha256"] != content_hash:
        raise BenchmarkError("model-visible input hash mismatch")
    if manifest["content_sha256"] != content_hash or manifest["patch_sha256"] != content_hash:
        raise BenchmarkError("manifest content or patch hash mismatch")
    source = manifest["source"]
    if source["source_type"] not in {"public", "synthetic"} or not source["redistribution_permitted"]:
        raise BenchmarkError("bundle is not redistributable public/synthetic input")
    if manifest["provenance_approval_status"] != "approved" or not source["license_spdx"]:
        raise BenchmarkError("bundle provenance or license is not approved")
    if any(manifest[flag] for flag in ("contains_proprietary_code", "contains_jira_context", "contains_database_context", "contains_private_context")):
        raise BenchmarkError("private, proprietary, Jira, or database input is forbidden")

    private_directory(workspace)
    try:
        for path in files:
            destination = workspace / path.relative_to(bundle)
            private_file(destination, path.read_bytes())
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    return {
        "workspace": str(workspace),
        "bundle_sha256": actual_bundle_sha256,
        "case_id": manifest["case_id"],
        "files_copied": len(files),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True, help="operator-provisioned local bundle directory")
    parser.add_argument("--workspace", type=Path, required=True, help="new private destination directory")
    parser.add_argument("--expected-bundle-sha256", required=True, help="operator-approved immutable tree hash")
    parser.add_argument("--max-files", type=int, default=16)
    parser.add_argument("--max-bytes", type=int, default=2_000_000)
    args = parser.parse_args()
    if args.max_files < 1 or args.max_bytes < 1:
        parser.error("limits must be positive")
    try:
        report = collect(args.bundle, args.workspace, args.expected_bundle_sha256, args.max_files, args.max_bytes)
    except (BenchmarkError, OSError) as exc:
        print(f"collect rejected: {exc}", file=sys.stderr)
        return 2
    print(__import__("json").dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
