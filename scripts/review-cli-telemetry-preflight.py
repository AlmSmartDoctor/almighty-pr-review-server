#!/usr/bin/env python3
"""Opt-in live probe for content-safe vendor CLI telemetry contracts.

The script never prints prompts, responses, commands, paths, stdout, or stderr. Its
output is a size-bounded schema containing only reviewed public names, counters,
booleans, public CLI/schema versions, and allowlisted safe error codes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.review.harness import (  # noqa: E402
    HarnessProfile,
    RuntimeCredentialError,
)
from server.review.vendor_telemetry import (  # noqa: E402
    MAX_EVENT_BYTES,
    MAX_FINAL_OUTPUT_BYTES,
    encode_preflight_report,
    event_schema,
    parse_claude_json,
    parse_codex_jsonl,
    public_cli_version,
    public_event_signature,
)
from server.review.vendors import (  # noqa: E402
    VendorTimeout,
    run_bounded_process,
)

_PROBE_TIMEOUT_SEC = 180
_VERSION_TIMEOUT_SEC = 15
_VERSION_STREAM_BYTES = 4 * 1024


def _run(args, *, stream_limit: int, **kwargs):
    return asyncio.run(
        run_bounded_process(args, stream_limit=stream_limit, **kwargs)
    )


def _version(binary: str) -> str | None:
    try:
        proc = _run(
            [binary, "--version"],
            stream_limit=_VERSION_STREAM_BYTES,
            timeout=_VERSION_TIMEOUT_SEC,
        )
    except (OSError, VendorTimeout):
        return None
    if proc.exit_code != 0 or proc.stdout_truncated or proc.stderr_truncated:
        return None
    value = proc.stdout.strip() or proc.stderr.strip()
    return public_cli_version(binary, value)


def _empty_signature() -> dict:
    return {
        "event_count": 0,
        "signatures": [],
        "unknown_key_count": 0,
        "unknown_event_type_count": 0,
        "signature_truncated": False,
        "unknown_count_truncated": False,
        "parse_partial": False,
    }


def _safe_failure(
    vendor: str,
    version: str | None,
    code: str,
    *,
    exit_code: int | None = None,
    stream_truncated: bool = False,
) -> dict:
    attestation = vendor == "claude"
    return {
        "vendor": vendor,
        "cli_version": version,
        "event_schema": event_schema(
            vendor, version, attestation=attestation
        ) if version else None,
        "exit_code": exit_code,
        "safe_error_code": code,
        **_empty_signature(),
        "final_output_present": False,
        "usage_present": False,
        "tool_calls_present": False,
        "telemetry_status": "unavailable",
        "stream_truncated": stream_truncated,
    }


def _read_bounded(path: Path) -> tuple[str, bool]:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_FINAL_OUTPUT_BYTES + 1)
    except OSError:
        return "", False
    truncated = len(data) > MAX_FINAL_OUTPUT_BYTES
    return data[:MAX_FINAL_OUTPUT_BYTES].decode("utf-8", "replace"), truncated


def _result(
    *,
    vendor: str,
    version: str,
    proc,
    parsed,
    signature: dict,
    final_output_present: bool,
    final_truncated: bool = False,
) -> dict:
    truncated = bool(
        proc.stdout_truncated or proc.stderr_truncated or final_truncated
    )
    return {
        "vendor": vendor,
        "cli_version": version,
        "event_schema": parsed.meta["event_schema"],
        "exit_code": proc.exit_code,
        "safe_error_code": (
            "output_limit" if truncated else parsed.meta["safe_error_code"]
        ),
        **signature,
        "final_output_present": bool(final_output_present) and not truncated,
        "usage_present": parsed.meta["input_tokens"] is not None,
        "tool_calls_present": parsed.meta["tool_calls"] is not None,
        "telemetry_status": (
            "partial" if truncated else parsed.meta["telemetry_status"]
        ),
        "stream_truncated": truncated,
    }


def _probe_codex(*, env, cwd: Path, runtime_dir: Path, model: str) -> dict:
    version = _version("codex")
    if version is None:
        return _safe_failure("codex", None, "unsupported_cli")
    last = runtime_dir / "codex-last-message.txt"
    args = [
        "codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
        "--ephemeral", "--ignore-user-config", "--ignore-rules", "--json",
        "--output-last-message", str(last), "--model", model,
        "-c", "model_reasoning_effort=low",
    ]
    try:
        proc = _run(
            args,
            input_text="Read probe.txt, then reply with exactly one word.",
            env=env,
            cwd=cwd,
            timeout=_PROBE_TIMEOUT_SEC,
            stream_limit=MAX_EVENT_BYTES,
        )
    except VendorTimeout:
        return _safe_failure("codex", version, "timeout")
    except OSError:
        return _safe_failure("codex", version, "process_exit")
    last_message, final_truncated = _read_bounded(last)
    if proc.stdout_truncated or proc.stderr_truncated or final_truncated:
        return _safe_failure(
            "codex", version, "output_limit",
            exit_code=proc.exit_code, stream_truncated=True,
        )
    try:
        parsed = parse_codex_jsonl(
            proc.stdout,
            last_message=last_message,
            cli_version=version,
            exit_code=proc.exit_code,
        )
    except ValueError:
        return _safe_failure(
            "codex", version, "invalid_output", exit_code=proc.exit_code
        )
    signature = public_event_signature(
        proc.stdout, vendor="codex", cli_version=version
    )
    return _result(
        vendor="codex",
        version=version,
        proc=proc,
        parsed=parsed,
        signature=signature,
        final_output_present=bool(parsed.output),
        final_truncated=final_truncated,
    )


def _probe_claude(*, env, cwd: Path, model: str) -> dict:
    version = _version("claude")
    if version is None:
        return _safe_failure("claude", None, "unsupported_cli")
    args = [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--no-session-persistence", "--allowedTools", "Read", "--model", model,
        "--effort", "low",
    ]
    try:
        proc = _run(
            args,
            input_text="Read probe.txt, then reply with exactly one word.",
            env=env,
            cwd=cwd,
            timeout=_PROBE_TIMEOUT_SEC,
            stream_limit=MAX_EVENT_BYTES,
        )
    except VendorTimeout:
        return _safe_failure("claude", version, "timeout")
    except OSError:
        return _safe_failure("claude", version, "process_exit")
    if proc.stdout_truncated or proc.stderr_truncated:
        return _safe_failure(
            "claude", version, "output_limit",
            exit_code=proc.exit_code, stream_truncated=True,
        )
    try:
        parsed = parse_claude_json(
            proc.stdout,
            cli_version=version,
            exit_code=proc.exit_code,
            attestation=True,
        )
    except ValueError:
        return _safe_failure(
            "claude", version, "invalid_output", exit_code=proc.exit_code
        )
    signature = public_event_signature(
        proc.stdout,
        vendor="claude",
        cli_version=version,
        attestation=True,
    )
    return _result(
        vendor="claude",
        version=version,
        proc=proc,
        parsed=parsed,
        signature=signature,
        final_output_present=(
            bool(parsed.output) and parsed.meta["status"] == "done"
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="run paid/authenticated probes")
    parser.add_argument("--vendor", choices=("all", "codex", "claude"), default="all")
    parser.add_argument("--codex-model", default="gpt-5.6-sol")
    parser.add_argument("--claude-model", default="sonnet")
    parser.add_argument("--output", help="write the sanitized report to this file")
    args = parser.parse_args()
    if not args.live:
        parser.error("live probes require explicit --live")

    hp = HarnessProfile.load("default")
    results = []
    selected = ("codex", "claude") if args.vendor == "all" else (args.vendor,)
    with tempfile.TemporaryDirectory(prefix="almighty-telemetry-preflight-") as raw:
        root = Path(raw)
        cwd = root / "synthetic-repo"
        cwd.mkdir()
        (cwd / "probe.txt").write_text("OK\n", encoding="utf-8")
        for vendor in selected:
            runtime_dir = root / f"{vendor}-runtime"
            try:
                with redirect_stdout(sys.stderr), hp.runtime_credentials(
                    runtime_dir=str(runtime_dir), vendor=vendor
                ):
                    env = hp.isolated_env(runtime_dir=str(runtime_dir))
                    if vendor == "codex":
                        result = _probe_codex(
                            env=env,
                            cwd=cwd,
                            runtime_dir=runtime_dir,
                            model=args.codex_model,
                        )
                    else:
                        result = _probe_claude(
                            env=env,
                            cwd=cwd,
                            model=args.claude_model,
                        )
            except RuntimeCredentialError as exc:
                result = _safe_failure(
                    vendor, _version(vendor), exc.safe_error_code
                )
            except Exception:
                # Auth/setup details may contain provider data. Emit classification only.
                result = _safe_failure(vendor, _version(vendor), "auth")
            results.append(result)

    report = encode_preflight_report(results)
    if args.output:
        Path(args.output).write_text(report + "\n", encoding="utf-8")
    else:
        print(report)
    return 0 if results and all(
        r["exit_code"] == 0
        and r["safe_error_code"] is None
        and r["telemetry_status"] == "ok"
        and not r["stream_truncated"]
        for r in results
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
