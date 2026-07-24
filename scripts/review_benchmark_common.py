"""Local-only helpers shared by the review benchmark tools.

The tools deliberately use this small validator instead of adding a runtime dependency.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from math import sqrt
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "benchmarks/review_pipeline/schema"
CLAIM_NORMALIZATION_VERSION = "claim-normalization-v1"
CLAIM_TOKENIZER_SHA256 = "9787d268171dfc88884d9961c1ae8608b111eda1eda892656a4db17622937458"
# Canonical UTF-8 JSON configuration pinned by the Task 3.1 artifact contract.
CLAIM_TOKENIZER_CONFIG = {
    "allowed_punctuation": ["-"], "case": "casefold", "normalization": "NFKC",
    "sort": "lexicographic", "stop_tokens": [], "whitespace": "collapse",
}
WILSON_Z_95 = 1.959963984540054


class BenchmarkError(ValueError):
    """A rejected benchmark artifact."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_claim(value: str) -> tuple[str, ...]:
    """Apply the pinned, deliberately non-fuzzy claim normalization algorithm."""
    if not isinstance(value, str):
        raise BenchmarkError("claim must be text")
    folded = unicodedata.normalize("NFKC", value).casefold()
    # Hyphen is the only allowed punctuation inside a token; every other punctuation
    # and all line ending/whitespace forms delimit tokens.
    tokens = re.findall(r"[^\W_]+(?:-[^\W_]+)*", folded, flags=re.UNICODE)
    stop_tokens = set(CLAIM_TOKENIZER_CONFIG["stop_tokens"])
    return tuple(sorted(token for token in tokens if token not in stop_tokens))


def canonical_claim_tokens(tokens: Any) -> tuple[str, ...]:
    """Validate an already-normalized stored claim key without accepting aliases."""
    if not isinstance(tokens, list) or not tokens:
        raise BenchmarkError("normalized claim tokens are required")
    if any(not isinstance(token, str) for token in tokens):
        raise BenchmarkError("normalized claim token must be text")
    normalized = normalize_claim(" ".join(tokens))
    if tuple(tokens) != normalized:
        raise BenchmarkError("normalized claim tokens are not canonical")
    return normalized


def wilson_95_lower_bound(numerator: int, denominator: int) -> float:
    if denominator < 0 or numerator < 0 or numerator > denominator:
        raise BenchmarkError("invalid Bernoulli metric counts")
    if denominator == 0:
        return 0.0
    point = numerator / denominator
    z2 = WILSON_Z_95 * WILSON_Z_95
    return (point + z2 / (2 * denominator) - WILSON_Z_95 * sqrt(
        point * (1 - point) / denominator + z2 / (4 * denominator * denominator)
    )) / (1 + z2 / denominator)


def required_denominator_for_wilson(*, numerator: int, denominator: int, lower_threshold: float) -> int:
    """Minimum extra all-success observations needed to reach a Wilson lower gate."""
    if denominator < 0 or numerator < 0 or numerator > denominator or not 0 <= lower_threshold <= 1:
        raise BenchmarkError("invalid Wilson sample requirement")
    if wilson_95_lower_bound(numerator, denominator) >= lower_threshold and denominator:
        return 0
    # A failed observation cannot be repaired by sampling; report at least one as a
    # locked shortfall rather than pretending a finite guaranteed sample exists.
    for total in range(max(1, denominator), 10_000_001):
        if wilson_95_lower_bound(numerator + total - denominator, total) >= lower_threshold:
            return total - denominator
    return 10_000_000


def bernoulli_metric(numerator: int, denominator: int, *, point_threshold: float, lower_threshold: float, minimum_denominator: int = 1) -> dict[str, Any]:
    lower = wilson_95_lower_bound(numerator, denominator)
    shortfall = required_denominator_for_wilson(
        numerator=numerator, denominator=denominator, lower_threshold=lower_threshold
    )
    if denominator < minimum_denominator:
        shortfall = max(shortfall, minimum_denominator - denominator)
    passed = denominator >= minimum_denominator and numerator / denominator >= point_threshold and lower >= lower_threshold
    return {
        "numerator": numerator, "denominator": denominator,
        "point_estimate": numerator / denominator if denominator else 0.0,
        "wilson_95_lower_bound": lower, "threshold": point_threshold,
        "passed": passed, "required_sample_shortfall": shortfall,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def strict_json_loads(value: str | bytes) -> Any:
    def no_duplicates(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise BenchmarkError("duplicate JSON key")
            result[key] = item
        return result

    try:
        return json.loads(value, object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError("invalid JSON") from exc


def load_json(path: Path) -> Any:
    try:
        return strict_json_loads(path.read_bytes())
    except (OSError, BenchmarkError) as exc:
        raise BenchmarkError(f"invalid JSON: {path}") from exc


def schema_for(name: str) -> dict[str, Any]:
    return load_json(SCHEMA_DIR / f"{name}.schema.json")


def _resolve_ref(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise BenchmarkError(f"unsupported schema reference: {reference}")
    node: Any = root
    for part in reference[2:].split("/"):
        node = node[part]
    return node


def _validate(schema: dict[str, Any], value: Any, root: dict[str, Any], location: str) -> None:
    if "$ref" in schema:
        _validate(_resolve_ref(root, schema["$ref"]), value, root, location)
        return
    if "const" in schema and value != schema["const"]:
        raise BenchmarkError(f"{location}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise BenchmarkError(f"{location}: value is not permitted")
    if "oneOf" in schema:
        matches = 0
        for candidate in schema["oneOf"]:
            try:
                _validate(candidate, value, root, location)
            except BenchmarkError:
                continue
            matches += 1
        if matches != 1:
            raise BenchmarkError(f"{location}: must match exactly one schema variant")
    expected = schema.get("type")
    object_schema = expected == "object" or "properties" in schema or "required" in schema
    if object_schema:
        if not isinstance(value, dict):
            raise BenchmarkError(f"{location}: expected object")
        properties = schema.get("properties", {})
        missing = set(schema.get("required", ())) - set(value)
        if missing:
            raise BenchmarkError(f"{location}: missing required field {sorted(missing)[0]}")
        if schema.get("additionalProperties") is False:
            unexpected = set(value) - set(properties)
            if unexpected:
                raise BenchmarkError(f"{location}: unexpected field {sorted(unexpected)[0]}")
        for key, child in properties.items():
            if key in value:
                _validate(child, value[key], root, f"{location}.{key}")
    elif expected == "array":
        if not isinstance(value, list):
            raise BenchmarkError(f"{location}: expected array")
        if len(value) < schema.get("minItems", 0):
            raise BenchmarkError(f"{location}: too few items")
        if schema.get("uniqueItems"):
            rendered = [canonical_json(item) for item in value]
            if len(set(rendered)) != len(rendered):
                raise BenchmarkError(f"{location}: duplicate items")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(schema["items"], item, root, f"{location}[{index}]")
    elif expected == "string":
        if not isinstance(value, str):
            raise BenchmarkError(f"{location}: expected string")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise BenchmarkError(f"{location}: invalid string length")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise BenchmarkError(f"{location}: string does not match required pattern")
        if schema.get("format") == "uri":
            parsed = urlparse(value)
            if not parsed.scheme or not parsed.netloc:
                raise BenchmarkError(f"{location}: invalid URI")
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise BenchmarkError(f"{location}: expected integer")
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise BenchmarkError(f"{location}: integer outside permitted range")
    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise BenchmarkError(f"{location}: expected number")
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise BenchmarkError(f"{location}: number outside permitted range")
    elif expected == "boolean" and not isinstance(value, bool):
        raise BenchmarkError(f"{location}: expected boolean")


def validate_schema(name: str, value: Any) -> None:
    schema = schema_for(name)
    _validate(schema, value, schema, name)
    # JSON Schema cannot express this ordering in the existing v1 schema.
    if name == "adjudication":
        if value["resolution_status"] == "unresolved":
            raise BenchmarkError("adjudication: unresolved gold answer")
        issue_ids = [issue["issue_id"] for issue in value["issues"]]
        if len(set(issue_ids)) != len(issue_ids):
            raise BenchmarkError("adjudication: duplicate issue ID")
        resolutions = value["prediction_issue_resolutions"]
        resolution_ids = [item["prediction_id"] for item in resolutions]
        if len(set(resolution_ids)) != len(resolution_ids):
            raise BenchmarkError("adjudication: duplicate prediction resolution")
        for resolution in resolutions:
            issue_id = resolution.get("issue_id")
            if resolution["status"] == "resolved" and issue_id not in issue_ids:
                raise BenchmarkError("adjudication: resolution issue is unknown")
            if resolution["status"] == "unmatched" and issue_id is not None:
                raise BenchmarkError("adjudication: unmatched resolution names issue")
        pair_ids = []
        for pair in value["issue_pairs"]:
            first = pair["first_prediction_id"]
            second = pair["second_prediction_id"]
            if first == second:
                raise BenchmarkError("adjudication: pair must name distinct predictions")
            pair_ids.append(tuple(sorted((first, second))))
        if len(set(pair_ids)) != len(pair_ids):
            raise BenchmarkError("adjudication: duplicate or conflicting pair label")
        verdicts = value["adjudicator_verdicts"]
        adjudicator_ids = [item["adjudicator_id"] for item in verdicts]
        if len(verdicts) < 2 or len(set(adjudicator_ids)) != len(verdicts):
            raise BenchmarkError("adjudication: two independent adjudicators required")
        verdict_values = {item["independent_verdict"] for item in verdicts}
        disagreement_values = {item["disagreement_status"] for item in verdicts}
        if value["resolution_status"] == "unanimous" and (
            verdict_values != {"accept"}
            or disagreement_values != {"none"}
        ):
            raise BenchmarkError("adjudication: unanimous verdicts conflict")
        if value["resolution_status"] == "resolved" and (
            "disputed" in disagreement_values
            or "resolved" not in disagreement_values
            or not isinstance(value.get("resolution_record"), dict)
        ):
            raise BenchmarkError("adjudication: resolver status is incomplete")
        for issue in value["issues"]:
            for item in issue["allowed_locations"]:
                if item["line_end"] < item["line_start"]:
                    raise BenchmarkError("adjudication: location line_end precedes line_start")
        for item in value["known_clean_ranges"]:
            if item["line_end"] < item["line_start"]:
                raise BenchmarkError("adjudication: clean range line_end precedes line_start")


def assert_safe_tree(root: Path, *, max_files: int | None = None, max_bytes: int | None = None) -> list[Path]:
    """Return regular files below root, rejecting links and traversal-capable inputs."""
    if root.is_symlink():
        raise BenchmarkError(f"symlink root is forbidden: {root}")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise BenchmarkError(f"not a directory: {root}")
    files: list[Path] = []
    total = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in dirs + names:
            item = current_path / name
            if item.is_symlink():
                raise BenchmarkError(f"symlinks are forbidden: {item}")
        for name in names:
            item = current_path / name
            info = item.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise BenchmarkError(f"non-regular file is forbidden: {item}")
            files.append(item)
            total += info.st_size
            if max_files is not None and len(files) > max_files:
                raise BenchmarkError("file count limit exceeded")
            if max_bytes is not None and total > max_bytes:
                raise BenchmarkError("byte limit exceeded")
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def tree_sha256(root: Path, files: list[Path] | None = None) -> str:
    root = root.resolve(strict=True)
    files = files if files is not None else assert_safe_tree(root)
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def private_directory(path: Path) -> None:
    path.mkdir(parents=False, mode=0o700)
    os.chmod(path, 0o700)


def private_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.chmod(path, 0o600)


def disjoint_paths(paths: list[Path]) -> None:
    resolved = [path.resolve(strict=True) for path in paths]
    for index, left in enumerate(resolved):
        for right in resolved[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise BenchmarkError("manifest, prediction, and adjudication roots must be physically separate")
