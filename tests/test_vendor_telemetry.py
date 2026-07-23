import json
import runpy
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from server.review.vendor_telemetry import (
    append_execution_attempt,
    build_execution_envelope,
    event_schema,
    normalize_legacy_output,
    parse_claude_json,
    parse_codex_jsonl,
    MAX_EVENT_LINES,
    MAX_PREFLIGHT_REPORT_BYTES,
    encode_preflight_report,
    public_event_signature,
    unavailable_meta,
    validate_execution_envelope,
    validate_meta,
)
from server.review.vendors import ProcessOutput


def _jsonl(*events):
    return "\n".join(json.dumps(event) for event in events)


def test_codex_exact_version_extracts_usage_and_tool_count_without_content():
    payload = _jsonl(
        {"type": "thread.started", "thread_id": "secret-thread"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "item-1",
                "type": "command_execution",
                "command": "cat /private/secret.txt",
                "aggregated_output": "SECRET-CONTENT",
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "item-2", "type": "agent_message", "text": "answer"},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
            },
        },
    )

    parsed = parse_codex_jsonl(
        payload,
        last_message='```json\n{"findings":[]}\n```',
        cli_version="codex-cli 0.144.5",
    )

    assert parsed.output.endswith("```")
    assert parsed.meta["telemetry_status"] == "ok"
    assert parsed.meta["tool_calls"] == 1
    assert parsed.meta["input_tokens"] == 100
    assert parsed.meta["cached_input_tokens"] == 40
    assert parsed.meta["output_tokens"] == 20
    assert parsed.meta["total_tokens"] == 120
    encoded = json.dumps(parsed.meta)
    assert "secret" not in encoded.lower()
    assert "command" not in encoded.lower()
    assert "aggregated_output" not in encoded


def test_codex_unknown_version_keeps_final_output_and_marks_unavailable():
    parsed = parse_codex_jsonl(
        _jsonl({"type": "turn.completed", "usage": {"input_tokens": 1}}),
        last_message="final",
        cli_version="codex-cli 999",
    )

    assert parsed.output == "final"
    assert parsed.meta["telemetry_status"] == "unavailable"
    assert parsed.meta["event_schema"] is None


def test_codex_unknown_event_is_partial_not_review_failure():
    parsed = parse_codex_jsonl(
        _jsonl(
            {"type": "future.event", "private": "do not persist"},
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        ),
        last_message="final",
        cli_version="codex-cli 0.144.5",
    )

    assert parsed.output == "final"
    assert parsed.meta["telemetry_status"] == "partial"
    assert "private" not in json.dumps(parsed.meta)


def test_claude_schema_is_attestation_only_until_separate_activation():
    version = "2.1.198 (Claude Code)"

    assert event_schema("claude", version) is None
    assert event_schema("claude", version, attestation=True) == "claude-json-v2.1.198"


def test_claude_extracts_result_usage_and_counts_tool_blocks():
    payload = _jsonl(
        {"type": "system", "subtype": "init", "cwd": "/private/work"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/private/work/a.py"},
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": '```json\n{"findings":[]}\n```',
            "usage": {
                "input_tokens": 30,
                "cache_read_input_tokens": 10,
                "output_tokens": 7,
            },
        },
    )

    parsed = parse_claude_json(
        payload,
        cli_version="2.1.198 (Claude Code)",
        attestation=True,
    )

    assert "findings" in parsed.output
    assert parsed.meta["status"] == "done"
    assert parsed.meta["tool_calls"] == 1
    assert parsed.meta["cached_input_tokens"] == 10
    assert "/private" not in json.dumps(parsed.meta)


def test_claude_error_maps_status_without_copying_error_body():
    payload = _jsonl(
        {"type": "assistant", "error": "credential and source text"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 403,
            "result": "sensitive provider error",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    )

    parsed = parse_claude_json(
        payload,
        cli_version="2.1.198 (Claude Code)",
        exit_code=1,
        attestation=True,
    )

    assert parsed.meta["status"] == "failed"
    assert parsed.meta["safe_error_code"] == "auth"
    assert "credential" not in json.dumps(parsed.meta)
    assert "sensitive" not in json.dumps(parsed.meta)


def test_public_signature_emits_only_exact_schema_allowlisted_names():
    payload = _jsonl(
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "cat secret"},
            "/private/dynamic-secret-key": "private",
        },
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
        {"type": "/private/secret-event", "credential-path": "hidden"},
    )

    summary = public_event_signature(
        payload, vendor="codex", cli_version="codex-cli 0.144.5"
    )

    encoded = json.dumps(summary)
    assert "item.completed" in encoded
    assert '"item"' in encoded
    assert "turn.completed" in encoded
    assert summary["unknown_key_count"] == 3
    assert summary["unknown_event_type_count"] == 1
    for forbidden in (
        "dynamic-secret", "credential-path", "secret-event", "cat secret", "private"
    ):
        assert forbidden not in encoded


def test_unknown_schema_suppresses_all_dynamic_type_and_key_names():
    payload = _jsonl(
        {"type": "SECRET_EVENT_NAME", "SECRET_PATH_KEY": "/private/token"}
    )

    summary = public_event_signature(
        payload, vendor="codex", cli_version="codex-cli 999"
    )

    encoded = json.dumps(summary)
    assert summary["signatures"] == []
    assert summary["unknown_event_type_count"] == 1
    assert summary["unknown_key_count"] == 2
    assert "SECRET" not in encoded and "/private" not in encoded


def test_signature_and_unknown_counts_have_hard_caps(monkeypatch):
    monkeypatch.setattr("server.review.vendor_telemetry.MAX_PUBLIC_SIGNATURES", 1)
    monkeypatch.setattr("server.review.vendor_telemetry.MAX_UNKNOWN_KEY_COUNT", 1)
    payload = _jsonl(
        {"type": "turn.started", "secret-a": 1, "secret-b": 2},
        {"type": "turn.completed", "usage": {}},
    )

    summary = public_event_signature(
        payload, vendor="codex", cli_version="codex-cli 0.144.5"
    )

    assert len(summary["signatures"]) == 1
    assert summary["signature_truncated"] is True
    assert summary["unknown_key_count"] == 1
    assert summary["unknown_count_truncated"] is True
    assert "secret" not in json.dumps(summary)


def test_signature_event_count_is_bounded_for_large_jsonl():
    payload = "\n".join(
        '{"type":"turn.started"}' for _ in range(MAX_EVENT_LINES + 100)
    )

    summary = public_event_signature(
        payload, vendor="codex", cli_version="codex-cli 0.144.5"
    )

    assert summary["event_count"] == MAX_EVENT_LINES
    assert summary["parse_partial"] is True
    assert len(summary["signatures"]) == 1


def test_preflight_report_schema_and_total_size_are_bounded():
    result = {
        "vendor": "codex", "cli_version": "codex-cli 0.144.5",
        "event_schema": "codex-jsonl-v0.144.5", "exit_code": 0,
        "safe_error_code": None, "event_count": 1,
        "signatures": [{"type": "turn.started", "keys": ["type"]}],
        "unknown_key_count": 0, "unknown_event_type_count": 0,
        "signature_truncated": False, "unknown_count_truncated": False,
        "parse_partial": False, "final_output_present": True,
        "usage_present": False, "tool_calls_present": True,
        "telemetry_status": "ok", "stream_truncated": False,
    }

    encoded = encode_preflight_report([result])

    assert len(encoded.encode()) < MAX_PREFLIGHT_REPORT_BYTES
    assert set(json.loads(encoded)) == {"schema_version", "results"}
    with pytest.raises(ValueError, match="schema"):
        encode_preflight_report([{**result, "secret_path": "/private/token"}])
    with pytest.raises(ValueError, match="schema/version mismatch"):
        encode_preflight_report([
            {**result, "cli_version": "codex-cli 999"}
        ])
    with pytest.raises(ValueError, match="public CLI version"):
        encode_preflight_report([
            {**result, "cli_version": "SECRET_WRAPPER_TEXT", "event_schema": None}
        ])


def test_preflight_rejects_non_public_version_output():
    script = Path(__file__).parents[1] / "scripts" / "review-cli-telemetry-preflight.py"
    namespace = runpy.run_path(str(script))
    globals_ = namespace["_version"].__globals__
    globals_["_run"] = lambda *args, **kwargs: ProcessOutput(
        stdout="SECRET_TOKEN_FROM_PATH", stderr="", exit_code=0, duration_ms=1
    )

    assert namespace["_version"]("codex") is None


def test_preflight_truncation_emits_only_output_limit_summary():
    script = Path(__file__).parents[1] / "scripts" / "review-cli-telemetry-preflight.py"
    namespace = runpy.run_path(str(script))
    parsed = normalize_legacy_output(
        "codex", "RAW_FINAL_SENTINEL", cli_version="codex-cli 0.144.5"
    )
    proc = ProcessOutput(
        stdout="RAW_STREAM_SENTINEL", stderr="", exit_code=0, duration_ms=1,
        stdout_truncated=True,
    )

    result = namespace["_result"](
        vendor="codex",
        version="codex-cli 0.144.5",
        proc=proc,
        parsed=parsed,
        signature=namespace["_empty_signature"](),
        final_output_present=True,
    )
    encoded = encode_preflight_report([result])

    assert result["safe_error_code"] == "output_limit"
    assert result["stream_truncated"] is True
    assert result["final_output_present"] is False
    assert "RAW_STREAM_SENTINEL" not in encoded
    assert "RAW_FINAL_SENTINEL" not in encoded


def test_preflight_output_file_contains_only_sanitized_schema(
    tmp_path, monkeypatch, capsys
):
    script = Path(__file__).parents[1] / "scripts" / "review-cli-telemetry-preflight.py"
    namespace = runpy.run_path(str(script))
    observed = []

    class Profile:
        @contextmanager
        def runtime_credentials(self, *, runtime_dir, vendor):
            observed.append(("enter", vendor))
            yield
            observed.append(("exit", vendor))

        def isolated_env(self, *, runtime_dir):
            return {}

    class Profiles:
        @staticmethod
        def load(name):
            return Profile()

    globals_ = namespace["main"].__globals__
    globals_["HarnessProfile"] = Profiles
    globals_["_probe_codex"] = lambda **kwargs: namespace["_safe_failure"](
        "codex", "codex-cli 999", "output_limit",
        exit_code=0, stream_truncated=True,
    )
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [str(script), "--live", "--vendor", "codex", "--output", str(output)],
    )

    assert namespace["main"]() == 1
    report = output.read_text(encoding="utf-8")
    assert capsys.readouterr().out == ""
    assert json.loads(report)["schema_version"] == 2
    assert observed == [("enter", "codex"), ("exit", "codex")]
    assert len(report.encode()) < MAX_PREFLIGHT_REPORT_BYTES


def test_meta_validator_rejects_unknown_or_invalid_values():
    meta = unavailable_meta("codex")
    validate_meta(meta)

    with pytest.raises(ValueError, match="unsupported keys"):
        validate_meta({**meta, "command": "cat secret"})
    with pytest.raises(ValueError, match="numeric"):
        validate_meta({**meta, "tool_calls": -1})
    with pytest.raises(ValueError, match="safe vendor error"):
        validate_meta({**meta, "safe_error_code": "raw provider message"})


def test_legacy_output_stays_compatible_with_unavailable_telemetry():
    parsed = normalize_legacy_output("claude", "plain final")

    assert parsed.output == "plain final"
    assert parsed.meta["telemetry_status"] == "unavailable"


def _chunk(index=0):
    return {
        "index": index,
        "status": "done",
        "safe_error_code": None,
        "duration_ms": 10,
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "output_tokens": 2,
        "reasoning_output_tokens": 1,
        "total_tokens": 3,
        "tool_calls": 1,
        "event_count": 4,
        "stream_truncated": False,
        "telemetry_status": "ok",
        "cli_name": "codex",
        "cli_version": "codex-cli 0.144.5",
        "event_schema": "codex-jsonl-v0.144.5",
        "chunk_hash": "a" * 64,
        "context_hash": "b" * 64,
        "chunker_version": "char-v1",
        "prompt_nonce": "1234abcd",
        "scope_reassigned": 0,
        "scope_rejected": 0,
        "duplicate_groups": 0,
    }


def _identity(**overrides):
    value = {
        "protocol_version": "legacy-v0", "vendor": "codex", "model": "m",
        "effort": "high", "prompt_hash": "1" * 64,
        "harness_config_hash": "2" * 64, "adapter_name": "adapter",
        "adapter_version": "v1", "adapter_config_hash": "3" * 64,
        "cli_version": "cli-v1", "event_schema_version": "schema-v1",
        "diff_hash": "4" * 64, "context_hash": "5" * 64,
        "chunker_version": "char-v1", "scope_policy_mode": "observe",
        "dedupe_policy_mode": "observe", "policy_decision_hash": "6" * 64,
        "policy_config_hash": "7" * 64,
    }
    value.update(overrides)
    return value


def test_execution_envelope_appends_attempts_without_overwrite():
    first = build_execution_envelope(
        identity=_identity(), attempt=1, phase="review", chunks=[_chunk()],
    )
    second = build_execution_envelope(
        identity=_identity(), attempt=2, phase="review", chunks=[_chunk()],
    )

    merged = append_execution_attempt(first, second)

    assert [item["attempt"] for item in merged["attempts"]] == [1, 2]
    validate_execution_envelope(merged)


def test_execution_envelope_rejects_raw_or_nonmonotonic_data():
    envelope = build_execution_envelope(
        identity=_identity(), attempt=1, phase="review", chunks=[_chunk()],
    )
    envelope["attempts"][0]["chunks"][0]["command"] = "cat secret"
    with pytest.raises(ValueError, match="chunk keys"):
        validate_execution_envelope(envelope)

    first = build_execution_envelope(
        identity=_identity(), attempt=1, phase="review", chunks=[_chunk()],
    )
    with pytest.raises(ValueError, match="strictly increasing"):
        append_execution_attempt(first, first)
