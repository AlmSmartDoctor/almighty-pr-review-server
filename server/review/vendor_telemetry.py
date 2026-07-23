"""Content-safe parsing for vendor CLI structured execution telemetry.

The parsers inspect transient event payloads to extract the final model message and
allowlisted numeric metadata. Event bodies, tool commands, paths, prompts, and model
messages are never copied into ``meta``.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

SCHEMA_VERSION = 1
EXECUTION_SCHEMA_VERSION = 2
EXECUTION_IDENTITY_FIELDS = (
    "protocol_version", "vendor", "model", "effort", "prompt_hash",
    "harness_config_hash", "adapter_name", "adapter_version",
    "adapter_config_hash", "cli_version", "event_schema_version",
    "diff_hash", "context_hash", "chunker_version", "scope_policy_mode",
    "dedupe_policy_mode", "policy_decision_hash", "policy_config_hash",
)
_EXECUTION_HASH_FIELDS = {
    "prompt_hash", "harness_config_hash", "adapter_config_hash", "diff_hash",
    "context_hash", "policy_decision_hash", "policy_config_hash",
}
MAX_EVENT_LINES = 20_000
MAX_EVENT_BYTES = 10 * 1024 * 1024
MAX_FINAL_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_META_BYTES = 64 * 1024
MAX_PUBLIC_SIGNATURES = 64
MAX_PUBLIC_KEY_LENGTH = 64
MAX_UNKNOWN_KEY_COUNT = 100_000
MAX_PREFLIGHT_REPORT_BYTES = 32 * 1024

# Production adapters may only enable schemas backed by a reviewed activation.
# Claude 2.1.198 remains attestation-only until a separate tested/approved commit.
_SUPPORTED_EVENT_SCHEMAS = {
    ("codex", "codex-cli 0.144.5"): "codex-jsonl-v0.144.5",
}
_ATTESTATION_EVENT_SCHEMAS = {
    ("claude", "2.1.198 (Claude Code)"): "claude-json-v2.1.198",
}
_PUBLIC_VERSION_PATTERNS = {
    "codex": re.compile(r"codex-cli [0-9][A-Za-z0-9.+-]{0,63}\Z"),
    "claude": re.compile(
        r"[0-9][A-Za-z0-9.+-]{0,63} \(Claude Code\)\Z"
    ),
}


def public_cli_version(vendor: str, value: str) -> str | None:
    pattern = _PUBLIC_VERSION_PATTERNS.get(vendor)
    normalized = value.strip() if isinstance(value, str) else ""
    return (
        normalized
        if normalized and pattern is not None and pattern.fullmatch(normalized)
        else None
    )


_SAFE_ERROR_CODES = {
    None,
    "auth",
    "rate_limit",
    "overloaded",
    "invalid_output",
    "output_limit",
    "unsupported_cli",
    "process_exit",
    "timeout",
    "canceled",
    "runtime_setup_failed",
    "runtime_cleanup_failed",
    "snapshot_cleanup_failed",
    "unknown",
}
_TOOL_ITEM_TYPES = {
    "command_execution",
    "mcp_tool_call",
    "web_search",
    "file_change",
}


@dataclass(frozen=True)
class ParsedVendorTelemetry:
    output: str
    meta: dict[str, Any]


def event_schema(
    vendor: str, cli_version: str | None, *, attestation: bool = False
) -> str | None:
    if not cli_version:
        return None
    key = (vendor, cli_version)
    schema = _SUPPORTED_EVENT_SCHEMAS.get(key)
    if schema is not None or not attestation:
        return schema
    return _ATTESTATION_EVENT_SCHEMAS.get(key)


def unavailable_meta(
    vendor: str,
    *,
    cli_version: str | None = None,
    status: str = "done",
    safe_error_code: str | None = None,
    exit_code: int | None = None,
    stream_truncated: bool = False,
) -> dict[str, Any]:
    meta = {
        "schema_version": SCHEMA_VERSION,
        "cli_name": vendor,
        "cli_version": cli_version,
        "event_schema": event_schema(vendor, cli_version),
        "status": status,
        "safe_error_code": safe_error_code,
        "exit_code": exit_code,
        "stream_truncated": bool(stream_truncated),
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
        "total_tokens": None,
        "tool_calls": None,
        "event_count": None,
        "telemetry_status": "unavailable",
    }
    validate_meta(meta)
    return meta


def normalize_legacy_output(
    vendor: str,
    output: str,
    *,
    cli_version: str | None = None,
) -> ParsedVendorTelemetry:
    _check_output_limit(output)
    return ParsedVendorTelemetry(
        output=output,
        meta=unavailable_meta(vendor, cli_version=cli_version),
    )


def parse_codex_jsonl(
    payload: str,
    *,
    last_message: str,
    cli_version: str,
    exit_code: int = 0,
    stream_truncated: bool = False,
) -> ParsedVendorTelemetry:
    schema = event_schema("codex", cli_version)
    if schema is None:
        return ParsedVendorTelemetry(
            output=_bounded_output(last_message),
            meta=unavailable_meta(
                "codex",
                cli_version=cli_version,
                status="done" if exit_code == 0 else "failed",
                safe_error_code=None if exit_code == 0 else "unsupported_cli",
                exit_code=exit_code,
                stream_truncated=stream_truncated,
            ),
        )
    events, parse_partial = _json_lines(payload)
    usage: dict[str, int] | None = None
    tool_calls = 0
    terminal_seen = False
    unknown_event = False
    safe_error_code = None
    for item in events:
        typ = item.get("type")
        if typ == "turn.completed":
            terminal_seen = True
            candidate = item.get("usage")
            if isinstance(candidate, dict):
                usage = _numeric_usage(candidate)
        elif typ == "item.completed":
            nested = item.get("item")
            if isinstance(nested, dict) and nested.get("type") in _TOOL_ITEM_TYPES:
                tool_calls += 1
        elif typ in {"turn.failed", "error"}:
            terminal_seen = True
            safe_error_code = _safe_error_from_event(item)
        elif typ not in {"thread.started", "turn.started", "item.started", "item.updated"}:
            unknown_event = True
    status = "done" if exit_code == 0 and terminal_seen and safe_error_code is None else "failed"
    if stream_truncated:
        status = "failed"
        safe_error_code = "output_limit"
    elif exit_code != 0 and safe_error_code is None:
        safe_error_code = "process_exit"
    telemetry_status = (
        "partial"
        if parse_partial or unknown_event or stream_truncated or usage is None
        else "ok"
    )
    meta = _meta(
        vendor="codex",
        cli_version=cli_version,
        schema=schema,
        status=status,
        safe_error_code=safe_error_code,
        exit_code=exit_code,
        stream_truncated=stream_truncated,
        usage=usage,
        tool_calls=tool_calls,
        event_count=len(events),
        telemetry_status=telemetry_status,
    )
    return ParsedVendorTelemetry(output=_bounded_output(last_message), meta=meta)


def parse_claude_json(
    payload: str,
    *,
    cli_version: str,
    exit_code: int = 0,
    stream_truncated: bool = False,
    attestation: bool = False,
) -> ParsedVendorTelemetry:
    schema = event_schema("claude", cli_version, attestation=attestation)
    if schema is None:
        return ParsedVendorTelemetry(
            output="",
            meta=unavailable_meta(
                "claude",
                cli_version=cli_version,
                status="done" if exit_code == 0 else "failed",
                safe_error_code=None if exit_code == 0 else "unsupported_cli",
                exit_code=exit_code,
                stream_truncated=stream_truncated,
            ),
        )
    events, parse_partial = _json_payload(payload)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    tool_calls = 0
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            tool_calls += sum(
                1 for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            )
    usage = _numeric_usage(result.get("usage")) if isinstance(result, dict) else None
    is_error = bool(result.get("is_error")) if isinstance(result, dict) else exit_code != 0
    status = "failed" if is_error or exit_code != 0 or stream_truncated else "done"
    safe_error_code = "output_limit" if stream_truncated else None
    if status == "failed" and safe_error_code is None:
        safe_error_code = _safe_error_from_status(
            result.get("api_error_status") if isinstance(result, dict) else None
        )
    output = result.get("result", "") if isinstance(result, dict) else ""
    if not isinstance(output, str):
        output = ""
        parse_partial = True
    telemetry_status = (
        "partial"
        if parse_partial or stream_truncated or result is None or usage is None
        else "ok"
    )
    meta = _meta(
        vendor="claude",
        cli_version=cli_version,
        schema=schema,
        status=status,
        safe_error_code=safe_error_code,
        exit_code=exit_code,
        stream_truncated=stream_truncated,
        usage=usage,
        tool_calls=tool_calls,
        event_count=len(events),
        telemetry_status=telemetry_status,
    )
    return ParsedVendorTelemetry(output=_bounded_output(output), meta=meta)


_PUBLIC_EVENT_KEYS = {
    "codex-jsonl-v0.144.5": {
        "thread.started": frozenset({"type", "thread_id"}),
        "turn.started": frozenset({"type"}),
        "item.started": frozenset({"type", "item"}),
        "item.updated": frozenset({"type", "item"}),
        "item.completed": frozenset({"type", "item"}),
        "turn.completed": frozenset({"type", "usage"}),
        "turn.failed": frozenset({"type", "code", "error"}),
        "error": frozenset({"type", "code", "error"}),
    },
    "claude-json-v2.1.198": {
        "system": frozenset({"type", "subtype", "cwd"}),
        "assistant": frozenset({"type", "message"}),
        "result": frozenset(
            {"type", "subtype", "is_error", "result", "usage", "api_error_status"}
        ),
    },
}


def public_event_signature(
    payload: str,
    *,
    vendor: str,
    cli_version: str,
    attestation: bool = False,
) -> dict[str, Any]:
    """Return only reviewed public type/key names for an exact CLI schema."""
    events, partial = _json_payload(payload)
    schema = event_schema(vendor, cli_version, attestation=attestation)
    allowed_by_type = _PUBLIC_EVENT_KEYS.get(schema, {})
    signatures = []
    seen = set()
    unknown_keys = 0
    unknown_types = 0
    signature_truncated = False
    unknown_count_truncated = False
    for event in events:
        typ = event.get("type")
        allowed = allowed_by_type.get(typ) if isinstance(typ, str) else None
        if allowed is None or len(typ) > MAX_PUBLIC_KEY_LENGTH:
            if unknown_types < MAX_UNKNOWN_KEY_COUNT:
                unknown_types += 1
            else:
                unknown_count_truncated = True
            allowed = frozenset()
            public_type = None
        else:
            public_type = typ
        public_keys = []
        for key in event:
            if (
                isinstance(key, str)
                and len(key) <= MAX_PUBLIC_KEY_LENGTH
                and key in allowed
            ):
                public_keys.append(key)
            elif unknown_keys < MAX_UNKNOWN_KEY_COUNT:
                unknown_keys += 1
            else:
                unknown_count_truncated = True
        if public_type is None:
            continue
        signature = (public_type, tuple(sorted(public_keys)))
        if signature in seen:
            continue
        seen.add(signature)
        if len(signatures) >= MAX_PUBLIC_SIGNATURES:
            signature_truncated = True
            continue
        signatures.append({"type": public_type, "keys": list(signature[1])})
    return {
        "event_count": len(events),
        "signatures": signatures,
        "unknown_key_count": unknown_keys,
        "unknown_event_type_count": unknown_types,
        "signature_truncated": signature_truncated,
        "unknown_count_truncated": unknown_count_truncated,
        "parse_partial": partial,
    }


_PREFLIGHT_RESULT_KEYS = {
    "vendor", "cli_version", "event_schema", "exit_code", "safe_error_code",
    "event_count", "signatures", "unknown_key_count", "unknown_event_type_count",
    "signature_truncated", "unknown_count_truncated", "parse_partial",
    "final_output_present", "usage_present", "tool_calls_present",
    "telemetry_status", "stream_truncated",
}


def encode_preflight_report(results: list[dict[str, Any]]) -> str:
    if not isinstance(results, list) or not 1 <= len(results) <= 2:
        raise ValueError("invalid preflight results")
    seen = set()
    for result in results:
        if not isinstance(result, dict) or set(result) != _PREFLIGHT_RESULT_KEYS:
            raise ValueError("invalid preflight result schema")
        vendor = result["vendor"]
        if vendor not in {"claude", "codex"} or vendor in seen:
            raise ValueError("invalid preflight vendor")
        seen.add(vendor)
        for key in ("cli_version", "event_schema"):
            value = result[key]
            if value is not None and (
                not isinstance(value, str) or len(value) > 128
            ):
                raise ValueError(f"invalid preflight {key}")
        if (
            result["cli_version"] is not None
            and public_cli_version(vendor, result["cli_version"])
            != result["cli_version"]
        ):
            raise ValueError("invalid public CLI version")
        expected_schema = (
            event_schema(
                vendor,
                result["cli_version"],
                attestation=(vendor == "claude"),
            )
            if result["cli_version"] is not None else None
        )
        if result["event_schema"] != expected_schema:
            raise ValueError("preflight schema/version mismatch")
        if result["safe_error_code"] not in _SAFE_ERROR_CODES:
            raise ValueError("invalid preflight safe error")
        if result["exit_code"] is not None and (
            not isinstance(result["exit_code"], int)
            or isinstance(result["exit_code"], bool)
        ):
            raise ValueError("invalid preflight exit code")
        for key in ("event_count", "unknown_key_count", "unknown_event_type_count"):
            value = result[key]
            if (
                not isinstance(value, int) or isinstance(value, bool)
                or value < 0 or value > MAX_UNKNOWN_KEY_COUNT
            ):
                raise ValueError(f"invalid preflight count: {key}")
        for key in (
            "signature_truncated", "unknown_count_truncated", "parse_partial",
            "final_output_present", "usage_present", "tool_calls_present",
            "stream_truncated",
        ):
            if not isinstance(result[key], bool):
                raise ValueError(f"invalid preflight boolean: {key}")
        if result["telemetry_status"] not in {"ok", "partial", "unavailable"}:
            raise ValueError("invalid preflight telemetry status")
        signatures = result["signatures"]
        if not isinstance(signatures, list) or len(signatures) > MAX_PUBLIC_SIGNATURES:
            raise ValueError("invalid preflight signatures")
        allowed_by_type = _PUBLIC_EVENT_KEYS.get(result["event_schema"], {})
        for signature in signatures:
            if not isinstance(signature, dict) or set(signature) != {"type", "keys"}:
                raise ValueError("invalid preflight signature")
            typ = signature["type"]
            keys = signature["keys"]
            if typ not in allowed_by_type or not isinstance(keys, list):
                raise ValueError("invalid preflight signature type")
            if keys != sorted(set(keys)) or any(
                key not in allowed_by_type[typ] for key in keys
            ):
                raise ValueError("invalid preflight signature keys")
    encoded = json.dumps(
        {"schema_version": 2, "results": results},
        separators=(",", ":"), sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > MAX_PREFLIGHT_REPORT_BYTES:
        raise ValueError("preflight report exceeds size cap")
    return encoded


def validate_meta(meta: dict[str, Any]) -> None:
    allowed = {
        "schema_version", "cli_name", "cli_version", "event_schema", "status",
        "safe_error_code", "exit_code", "stream_truncated", "input_tokens",
        "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
        "total_tokens", "tool_calls", "event_count", "telemetry_status",
    }
    if set(meta) != allowed:
        raise ValueError("vendor telemetry contains unsupported keys")
    if meta["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported vendor telemetry schema")
    if meta["cli_name"] not in {"claude", "codex"}:
        raise ValueError("invalid vendor telemetry cli")
    if meta["status"] not in {"done", "failed", "timeout", "canceled"}:
        raise ValueError("invalid vendor telemetry status")
    if meta["telemetry_status"] not in {"ok", "partial", "unavailable"}:
        raise ValueError("invalid telemetry availability")
    if meta["safe_error_code"] not in _SAFE_ERROR_CODES:
        raise ValueError("invalid safe vendor error code")
    for key in (
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens", "total_tokens", "tool_calls", "event_count",
    ):
        value = meta[key]
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError(f"invalid numeric telemetry: {key}")
    if meta["exit_code"] is not None and (
        not isinstance(meta["exit_code"], int) or isinstance(meta["exit_code"], bool)
    ):
        raise ValueError("invalid telemetry exit code")
    for key in ("cli_version", "event_schema"):
        value = meta[key]
        if value is not None and (not isinstance(value, str) or len(value) > 128):
            raise ValueError(f"invalid telemetry string: {key}")
    if not isinstance(meta["stream_truncated"], bool):
        raise ValueError("invalid telemetry truncation flag")
    encoded = json.dumps(meta, separators=(",", ":"), sort_keys=True).encode()
    if len(encoded) > MAX_META_BYTES:
        raise ValueError("vendor telemetry exceeds size cap")


def _meta(
    *,
    vendor: str,
    cli_version: str,
    schema: str,
    status: str,
    safe_error_code: str | None,
    exit_code: int,
    stream_truncated: bool,
    usage: dict[str, int] | None,
    tool_calls: int,
    event_count: int,
    telemetry_status: str,
) -> dict[str, Any]:
    usage = usage or {}
    input_tokens = usage.get("input_tokens")
    cached = usage.get("cached_input_tokens", usage.get("cache_read_input_tokens"))
    output_tokens = usage.get("output_tokens")
    reasoning = usage.get("reasoning_output_tokens")
    numbers = [value for value in (input_tokens, output_tokens) if isinstance(value, int)]
    total = sum(numbers) if len(numbers) == 2 else None
    meta = {
        "schema_version": SCHEMA_VERSION,
        "cli_name": vendor,
        "cli_version": cli_version,
        "event_schema": schema,
        "status": status,
        "safe_error_code": safe_error_code,
        "exit_code": exit_code,
        "stream_truncated": bool(stream_truncated),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning,
        "total_tokens": total,
        "tool_calls": tool_calls,
        "event_count": event_count,
        "telemetry_status": telemetry_status,
    }
    validate_meta(meta)
    return meta


def _numeric_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    allowed = {
        "input_tokens", "cached_input_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "output_tokens", "reasoning_output_tokens",
    }
    out = {}
    for key in allowed:
        candidate = value.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            out[key] = candidate
    return out or None


def _bounded_event_payload(payload: str) -> tuple[str, bool]:
    encoded = payload.encode("utf-8", "replace")
    if len(encoded) <= MAX_EVENT_BYTES:
        return payload, False
    return encoded[:MAX_EVENT_BYTES].decode("utf-8", "ignore"), True


def _json_lines(payload: str) -> tuple[list[dict[str, Any]], bool]:
    payload, partial = _bounded_event_payload(payload)
    events = []
    for index, line in enumerate(io.StringIO(payload)):
        if index >= MAX_EVENT_LINES:
            partial = True
            break
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            partial = True
            continue
        if isinstance(item, dict):
            events.append(item)
        else:
            partial = True
    return events, partial


def _json_payload(payload: str) -> tuple[list[dict[str, Any]], bool]:
    payload, byte_partial = _bounded_event_payload(payload)
    stripped = payload.strip()
    if not stripped:
        return [], True
    try:
        one = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        events, line_partial = _json_lines(payload)
        return events, byte_partial or line_partial
    if isinstance(one, dict):
        return [one], byte_partial
    if isinstance(one, list) and all(isinstance(item, dict) for item in one):
        return one[:MAX_EVENT_LINES], byte_partial or len(one) > MAX_EVENT_LINES
    return [], True


def _safe_error_from_event(event: dict[str, Any]) -> str:
    code = event.get("code")
    if isinstance(code, str):
        lowered = code.lower()
        if "rate" in lowered:
            return "rate_limit"
        if "auth" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
            return "auth"
        if "overload" in lowered:
            return "overloaded"
    return "process_exit"


def _safe_error_from_status(status: Any) -> str:
    if status in {401, 403}:
        return "auth"
    if status == 429:
        return "rate_limit"
    if status in {529, 503}:
        return "overloaded"
    return "process_exit"


def build_execution_envelope(
    *,
    identity: dict[str, str],
    attempt: int,
    phase: str,
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(identity, dict) or set(identity) != set(EXECUTION_IDENTITY_FIELDS):
        raise ValueError("invalid vendor execution identity")
    envelope = {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        **identity,
        "attempts": [
            {
                "attempt": attempt,
                "phase": phase,
                "chunks": chunks,
            }
        ],
    }
    validate_execution_envelope(envelope)
    return envelope


def append_execution_attempt(
    existing: dict[str, Any] | None,
    addition: dict[str, Any],
) -> dict[str, Any]:
    validate_execution_envelope(addition)
    if existing is None:
        return addition
    validate_execution_envelope(existing)
    if any(
        existing.get(key) != addition.get(key)
        for key in ("schema_version", *EXECUTION_IDENTITY_FIELDS)
    ):
        raise ValueError("vendor execution envelope identity mismatch")
    attempts = [*existing["attempts"], *addition["attempts"]]
    merged = {**existing, "attempts": attempts}
    validate_execution_envelope(merged)
    return merged


def validate_execution_envelope(value: dict[str, Any]) -> None:
    expected = {"schema_version", "attempts", *EXECUTION_IDENTITY_FIELDS}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("invalid vendor execution envelope")
    if value["schema_version"] != EXECUTION_SCHEMA_VERSION:
        raise ValueError("unsupported vendor execution envelope schema")
    limits = {
        "protocol_version": 64, "vendor": 16, "model": 128, "effort": 32,
        "adapter_name": 256, "adapter_version": 128, "cli_version": 128,
        "event_schema_version": 128, "chunker_version": 128,
        "scope_policy_mode": 16, "dedupe_policy_mode": 16,
    }
    for key, limit in limits.items():
        item = value[key]
        if not isinstance(item, str) or len(item) > limit:
            raise ValueError(f"invalid execution envelope {key}")
    if value["vendor"] not in {"claude", "codex"}:
        raise ValueError("invalid execution envelope vendor")
    for key in ("scope_policy_mode", "dedupe_policy_mode"):
        if value[key] not in {"observe", "enforce"}:
            raise ValueError(f"invalid execution envelope {key}")
    for key in _EXECUTION_HASH_FIELDS:
        item = value[key]
        if not isinstance(item, str) or len(item) != 64 or any(
            ch not in "0123456789abcdef" for ch in item
        ):
            raise ValueError(f"invalid execution envelope {key}")
    attempts = value["attempts"]
    if not isinstance(attempts, list) or len(attempts) > 20:
        raise ValueError("invalid execution attempts")
    previous = 0
    for record in attempts:
        if not isinstance(record, dict) or set(record) != {"attempt", "phase", "chunks"}:
            raise ValueError("invalid execution attempt")
        attempt = record["attempt"]
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= previous:
            raise ValueError("execution attempts must be strictly increasing")
        previous = attempt
        if record["phase"] not in {"review", "verify"}:
            raise ValueError("invalid execution phase")
        chunks = record["chunks"]
        if not isinstance(chunks, list) or len(chunks) > 1_000:
            raise ValueError("invalid execution chunks")
        for chunk in chunks:
            _validate_execution_chunk(chunk)
    if len(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()) > MAX_META_BYTES:
        raise ValueError("vendor execution envelope exceeds size cap")


def _validate_execution_chunk(chunk: Any) -> None:
    allowed = {
        "index", "status", "safe_error_code", "duration_ms", "input_tokens",
        "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
        "total_tokens", "tool_calls", "event_count", "stream_truncated",
        "telemetry_status", "cli_name", "cli_version", "event_schema",
        "chunk_hash", "context_hash", "chunker_version", "prompt_nonce",
        "scope_reassigned", "scope_rejected", "duplicate_groups",
    }
    if not isinstance(chunk, dict) or set(chunk) != allowed:
        raise ValueError("invalid execution chunk keys")
    if not isinstance(chunk["index"], int) or isinstance(chunk["index"], bool) or chunk["index"] < 0:
        raise ValueError("invalid execution chunk index")
    if chunk["status"] not in {"done", "failed", "timeout", "canceled"}:
        raise ValueError("invalid execution chunk status")
    if chunk["safe_error_code"] not in _SAFE_ERROR_CODES:
        raise ValueError("invalid execution chunk error")
    if chunk["telemetry_status"] not in {"ok", "partial", "unavailable"}:
        raise ValueError("invalid execution chunk telemetry status")
    if not isinstance(chunk["stream_truncated"], bool):
        raise ValueError("invalid execution chunk truncation")
    for key in (
        "duration_ms", "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens", "total_tokens", "tool_calls", "event_count",
        "scope_reassigned", "scope_rejected", "duplicate_groups",
    ):
        item = chunk[key]
        if item is not None and (
            not isinstance(item, int) or isinstance(item, bool) or item < 0
        ):
            raise ValueError(f"invalid execution chunk numeric field: {key}")
    for key in (
        "cli_name", "cli_version", "event_schema", "chunk_hash",
        "context_hash", "chunker_version",
    ):
        item = chunk[key]
        if item is not None and (not isinstance(item, str) or len(item) > 128):
            raise ValueError(f"invalid execution chunk string field: {key}")
    nonce = chunk["prompt_nonce"]
    if not isinstance(nonce, str) or len(nonce) != 8 or any(
        ch not in "0123456789abcdef" for ch in nonce
    ):
        raise ValueError("invalid execution chunk prompt nonce")


def _bounded_output(output: str) -> str:
    _check_output_limit(output)
    return output


def _check_output_limit(output: str) -> None:
    if not isinstance(output, str):
        raise ValueError("vendor final output must be text")
    if len(output.encode("utf-8", "replace")) > MAX_FINAL_OUTPUT_BYTES:
        raise ValueError("vendor final output exceeds size cap")
