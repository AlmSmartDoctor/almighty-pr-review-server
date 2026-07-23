import asyncio
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from server.models import Finding
from server.review.findings_schema import PROMPT_SCHEMA_HINT, parse_findings
from server.review.harness import HarnessProfile
from server.review.vendor_telemetry import (
    ParsedVendorTelemetry,
    event_schema,
    normalize_legacy_output,
    parse_claude_json,
    parse_codex_jsonl,
    public_cli_version,
    unavailable_meta,
)

VENDOR_TIMEOUT_SEC = 600  # 벤더별 상한(rate-limit/hang 방어)

# codex reasoning effort 유효값(codex-cli 0.144.1 API가 실증한 enum; docs/vendor-cli-contract.md).
# 이 집합 밖 값이면 codex가 400으로 리뷰 전체를 깨므로, 모르는 값은 플래그 생략(codex 기본).
_CODEX_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
# claude --effort 유효값(명시적으로 유효값만 전달; 그 밖의 값은 플래그 생략).
_CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


_MAX_STREAM_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ProcessOutput:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass(frozen=True)
class VendorExecution:
    output: str
    status: str
    safe_error_code: str | None
    exit_code: int | None
    cli_name: str
    cli_version: str | None
    event_schema: str | None
    stream_truncated: bool
    telemetry: dict
    duration_ms: int | None = None

    def to_meta(self) -> dict:
        return {
            **self.telemetry,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class VendorVerifyResult:
    verdict: object
    execution: VendorExecution

    @property
    def refuted(self):
        return self.verdict.refuted

    @property
    def rationale(self):
        return self.verdict.rationale


@dataclass(frozen=True)
class VendorReviewResult:
    findings: list[Finding]
    execution: VendorExecution

    # Existing tests and fake pipeline paths treat review() as a sequence.
    def __iter__(self):
        return iter(self.findings)

    def __len__(self):
        return len(self.findings)

    def __getitem__(self, index):
        return self.findings[index]


def _read_file_bounded(path: Path, limit: int) -> tuple[bytes, bool]:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return b"", False
    return data[:limit], len(data) > limit


class VendorTimeout(RuntimeError):
    safe_error_code = "timeout"


class VendorProcessError(RuntimeError):
    def __init__(self, execution: VendorExecution):
        self.execution = execution
        self.safe_error_code = execution.safe_error_code or "process_exit"
        super().__init__(f"vendor process failed ({self.safe_error_code})")


async def _drain_limited(stream, limit: int) -> tuple[bytes, bool]:
    kept = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        remaining = limit - len(kept)
        if remaining > 0:
            kept.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    return bytes(kept), truncated


def _kill_process_group(proc) -> None:
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def run_bounded_process_sync(
    args, *, timeout: float, stream_limit: int, env=None, cwd=None,
    input_text: str | None = None,
) -> ProcessOutput:
    """Synchronous bounded runner for small control-plane probes such as --version."""
    if not isinstance(stream_limit, int) or isinstance(stream_limit, bool) or stream_limit < 1:
        raise ValueError("stream_limit must be a positive integer")
    started_at = time.monotonic()
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=(os.name == "posix"),
    )
    results = {}

    def drain(name, stream):
        kept = bytearray()
        truncated = False
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                remaining = stream_limit - len(kept)
                if remaining > 0:
                    kept.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
        except (OSError, ValueError):
            truncated = True
        results[name] = (bytes(kept), truncated)

    threads = [
        threading.Thread(target=drain, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", proc.stderr), daemon=True),
    ]

    def write_input():
        try:
            proc.stdin.write(input_text.encode("utf-8", "replace"))
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    if input_text is not None:
        threads.append(threading.Thread(target=write_input, daemon=True))
    for thread in threads:
        thread.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.wait()
        raise
    finally:
        for thread in threads:
            thread.join(timeout=1)
        still_running = any(thread.is_alive() for thread in threads)
        if still_running:
            proc.stdout.close()
            proc.stderr.close()
            for thread in threads:
                thread.join(timeout=1)
    out, out_cut = results.get("stdout", (b"", still_running))
    err, err_cut = results.get("stderr", (b"", still_running))
    return ProcessOutput(
        stdout=out.decode(errors="replace"),
        stderr=err.decode(errors="replace"),
        exit_code=proc.returncode,
        duration_ms=int((time.monotonic() - started_at) * 1000),
        stdout_truncated=out_cut,
        stderr_truncated=err_cut,
    )


async def _write_stdin(stream, payload: bytes | None) -> None:
    if stream is None or payload is None:
        return
    try:
        stream.write(payload)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        try:
            stream.close()
            await stream.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def run_bounded_process(
    args, env=None, cwd=None, timeout=None, input_text=None,
    *, stream_limit: int = _MAX_STREAM_BYTES,
) -> ProcessOutput:
    """Run a subprocess with concurrent, byte-bounded stdout/stderr drains."""
    if not isinstance(stream_limit, int) or isinstance(stream_limit, bool) or stream_limit < 1:
        raise ValueError("stream_limit must be a positive integer")
    started_at = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=env,
        cwd=cwd,
        stdin=(asyncio.subprocess.PIPE if input_text is not None
               else asyncio.subprocess.DEVNULL),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )

    async def collect():
        payload = input_text.encode() if input_text is not None else None
        writer = asyncio.create_task(_write_stdin(proc.stdin, payload))
        stdout = asyncio.create_task(_drain_limited(proc.stdout, stream_limit))
        stderr = asyncio.create_task(_drain_limited(proc.stderr, stream_limit))
        tasks = (writer, stdout, stderr)
        try:
            await proc.wait()
            await writer
            return await stdout, await stderr
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        ((out, out_cut), (err, err_cut)) = await asyncio.wait_for(
            collect(), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        _kill_process_group(proc)
        await proc.wait()
        raise VendorTimeout(f"vendor timeout after {timeout}s") from exc
    except asyncio.CancelledError:
        _kill_process_group(proc)
        await proc.wait()
        raise
    return ProcessOutput(
        stdout=out.decode(errors="replace"),
        stderr=err.decode(errors="replace"),
        exit_code=proc.returncode,
        duration_ms=int((time.monotonic() - started_at) * 1000),
        stdout_truncated=out_cut,
        stderr_truncated=err_cut,
    )


async def _default_runner(
    args, env=None, cwd=None, timeout=None, input_text=None
) -> ProcessOutput:
    """Production adapter runner using the shared bounded stream contract."""
    return await run_bounded_process(
        args, env=env, cwd=cwd, timeout=timeout, input_text=input_text
    )


def _cli_version(binary: str) -> str | None:
    try:
        result = run_bounded_process_sync(
            [binary, "--version"], timeout=15, stream_limit=4 * 1024
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.stdout_truncated or result.stderr_truncated:
        return None
    value = result.stdout.strip() or result.stderr.strip()
    return public_cli_version(binary, value) if result.exit_code == 0 else None


class _BaseAdapter:
    vendor = ""
    adapter_version = "review-adapter-v1"

    def __init__(self, runner=_default_runner, timeout=VENDOR_TIMEOUT_SEC):
        self._run = runner
        self._timeout = timeout

    def _build_argv(self, hp: HarnessProfile) -> list[str]:
        raise NotImplementedError

    def probe_cli_version(self) -> str:
        return _cli_version(self.vendor) or "unknown"

    def execution_contract(
        self, hp: HarnessProfile, *, cli_version: str | None = None
    ) -> dict[str, str]:
        cli_version = cli_version or self.probe_cli_version()
        return {
            "adapter_name": f"{type(self).__module__}.{type(self).__qualname__}",
            "adapter_version": self.adapter_version,
            "cli_version": cli_version,
            "event_schema_version": event_schema(self.vendor, cli_version)
            or "unavailable",
        }

    def _review_input(self, hp: HarnessProfile, prompt: str) -> str:
        return f"{hp.system_prompt_for(self.vendor)}\n\n{prompt}\n\n{PROMPT_SCHEMA_HINT}"

    def review_execution_inputs(
        self, hp: HarnessProfile, prompts: list[str], *, cli_version: str | None = None
    ) -> dict[str, list[str]]:
        placeholder = (
            Path("__ALMIGHTY_RUNTIME_LAST_MESSAGE__")
            if self.vendor == "codex" else None
        )
        return {
            "wire_prompts": [self._review_input(hp, prompt) for prompt in prompts],
            "review_argv": self._build_review_argv(
                hp, last_message_path=placeholder,
                cli_version=cli_version or self.probe_cli_version(),
            ),
        }

    def _build_review_argv(
        self, hp: HarnessProfile, *, last_message_path: Path | None,
        cli_version: str | None = None,
    ) -> list[str]:
        return self._build_argv(hp)

    @staticmethod
    def _from_parsed(
        parsed: ParsedVendorTelemetry,
        *,
        raw: ProcessOutput | None,
    ) -> VendorExecution:
        meta = parsed.meta
        return VendorExecution(
            output=parsed.output,
            status=meta["status"],
            safe_error_code=meta["safe_error_code"],
            exit_code=meta["exit_code"],
            cli_name=meta["cli_name"],
            cli_version=meta["cli_version"],
            event_schema=meta["event_schema"],
            stream_truncated=meta["stream_truncated"],
            telemetry=meta,
            duration_ms=raw.duration_ms if raw is not None else None,
        )

    async def _legacy_execution(
        self, raw, *, cli_version: str | None = None
    ) -> VendorExecution:
        version = cli_version or self.probe_cli_version()
        normalized_version = None if version == "unknown" else version
        if isinstance(raw, str):
            parsed = normalize_legacy_output(
                self.vendor, raw, cli_version=normalized_version
            )
            return self._from_parsed(parsed, raw=None)
        if not isinstance(raw, ProcessOutput):
            raise TypeError("vendor runner must return text or ProcessOutput")
        if (
            raw.exit_code == 0
            and not raw.stdout_truncated
            and not raw.stderr_truncated
        ):
            parsed = normalize_legacy_output(
                self.vendor, raw.stdout, cli_version=normalized_version
            )
        else:
            parsed = ParsedVendorTelemetry(
                output="",
                meta=unavailable_meta(
                    self.vendor,
                    cli_version=normalized_version,
                    status="failed",
                    safe_error_code=(
                        "output_limit"
                        if raw.stdout_truncated or raw.stderr_truncated
                        else "process_exit"
                    ),
                    exit_code=raw.exit_code,
                    stream_truncated=raw.stdout_truncated or raw.stderr_truncated,
                ),
            )
        return self._from_parsed(parsed, raw=raw)

    async def _review_execution(
        self, raw, *, last_message_path: Path | None,
        cli_version: str | None = None,
    ) -> VendorExecution:
        return await self._legacy_execution(raw, cli_version=cli_version)

    @staticmethod
    def _require_success(execution: VendorExecution) -> str:
        if execution.status != "done":
            raise VendorProcessError(execution)
        return execution.output

    async def complete(
        self,
        *,
        prompt: str,
        system_prompt: str,
        workdir: Path,
        harness: HarnessProfile,
        runtime_dir: str,
    ) -> str:
        """Run a read-only, isolated free-form task with this vendor.

        Review finding parsing stays in review(); Ground Truth Wiki generation uses this
        narrower primitive and validates its own structured output.
        """
        full = f"{system_prompt}\n\n{prompt}"
        cli_version = self.probe_cli_version()
        env = harness.isolated_env(runtime_dir=runtime_dir)
        raw = await self._run(
            self._build_argv(harness),
            env=env,
            cwd=str(workdir),
            timeout=self._timeout,
            input_text=full,
        )
        execution = await self._legacy_execution(
            raw, cli_version=cli_version
        )
        return self._require_success(execution)

    async def review(
        self,
        *,
        prompt: str,
        workdir: Path,
        harness: HarnessProfile,
        runtime_dir: str,
        raw_sink=None,
        cli_version: str | None = None,
    ) -> VendorReviewResult:
        full = self._review_input(harness, prompt)
        cli_version = cli_version or self.probe_cli_version()
        env = harness.isolated_env(runtime_dir=runtime_dir)  # ★개정: allowlist env
        last_message_path = (
            Path(runtime_dir) / f"{self.vendor}-last-{uuid.uuid4().hex}.txt"
            if self.vendor == "codex"
            else None
        )
        try:
            raw = await self._run(
                self._build_review_argv(
                    harness, last_message_path=last_message_path,
                    cli_version=cli_version,
                ),
                env=env,
                cwd=str(workdir),
                timeout=self._timeout,
                input_text=full,
            )
            execution = await self._review_execution(
                raw, last_message_path=last_message_path,
                cli_version=cli_version,
            )
        finally:
            if last_message_path is not None:
                try:
                    last_message_path.unlink(missing_ok=True)
                except OSError:
                    pass
        out = self._require_success(execution)
        if raw_sink:  # legacy opt-in diagnostic hook; production does not pass it.
            raw_sink(out)
        try:
            findings = parse_findings(out, vendor=self.vendor)
        except Exception as exc:
            failed_meta = {
                **execution.telemetry,
                "status": "failed",
                "safe_error_code": "invalid_output",
            }
            failed = VendorExecution(
                output="",
                status="failed",
                safe_error_code="invalid_output",
                exit_code=execution.exit_code,
                cli_name=execution.cli_name,
                cli_version=execution.cli_version,
                event_schema=execution.event_schema,
                stream_truncated=execution.stream_truncated,
                telemetry=failed_meta,
                duration_ms=execution.duration_ms,
            )
            raise VendorProcessError(failed) from exc
        return VendorReviewResult(findings=findings, execution=execution)

    async def verify(
        self, *, prompt: str, workdir: Path, harness: HarnessProfile, runtime_dir: str
    ):
        from server.review.verify import VERIFY_SCHEMA_HINT, parse_verdict

        cli_version = self.probe_cli_version()
        full = f"{harness.system_prompt_for(self.vendor)}\n\n{prompt}\n\n{VERIFY_SCHEMA_HINT}"
        env = harness.isolated_env(runtime_dir=runtime_dir)
        last_message_path = (
            Path(runtime_dir) / f"{self.vendor}-verify-last-{uuid.uuid4().hex}.txt"
            if self.vendor == "codex" else None
        )
        try:
            raw = await self._run(
                self._build_review_argv(
                    harness, last_message_path=last_message_path,
                    cli_version=cli_version,
                ),
                env=env,
                cwd=str(workdir),
                timeout=self._timeout,
                input_text=full,
            )
            execution = await self._review_execution(
                raw, last_message_path=last_message_path,
                cli_version=cli_version,
            )
        finally:
            if last_message_path is not None:
                try:
                    last_message_path.unlink(missing_ok=True)
                except OSError:
                    pass
        verdict = parse_verdict(self._require_success(execution))
        return VendorVerifyResult(verdict=verdict, execution=execution)


class ClaudeAdapter(_BaseAdapter):
    vendor = "claude"

    def _build_review_argv(
        self, hp, *, last_message_path, cli_version=None
    ):
        argv = self._build_argv(hp)
        version = cli_version or self.probe_cli_version()
        if event_schema(self.vendor, version):
            argv += ["--output-format", "stream-json", "--verbose"]
        return argv

    async def _review_execution(
        self, raw, *, last_message_path, cli_version=None
    ):
        version = cli_version or self.probe_cli_version()
        if isinstance(raw, str):
            return await self._legacy_execution(raw, cli_version=version)
        if not isinstance(raw, ProcessOutput):
            raise TypeError("vendor runner must return text or ProcessOutput")
        if not raw.stdout.lstrip().startswith("{"):
            return await self._legacy_execution(raw, cli_version=version)
        parsed = parse_claude_json(
            raw.stdout,
            cli_version="" if version == "unknown" else version,
            exit_code=raw.exit_code,
            stream_truncated=raw.stdout_truncated or raw.stderr_truncated,
        )
        return self._from_parsed(parsed, raw=raw)

    def _build_argv(self, hp):
        # argv는 docs/vendor-cli-contract.md(Task 0.5)에서 실증된 값.
        tools = ",".join(hp.claude_allowed_tools)
        argv = [
            "claude",
            "-p",
            "--allowedTools",
            tools,
            "--model",
            hp.model,
        ]
        # claude도 --effort로 reasoning 강도를 받는다(유효값만 주입).
        if hp.effort in _CLAUDE_EFFORTS:
            argv += ["--effort", hp.effort]
        return argv


class CodexAdapter(_BaseAdapter):
    vendor = "codex"

    def _build_review_argv(
        self, hp, *, last_message_path, cli_version=None
    ):
        argv = self._build_argv(hp)
        version = cli_version or self.probe_cli_version()
        if last_message_path is not None and event_schema(self.vendor, version):
            argv += [
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--json",
                "--output-last-message",
                str(last_message_path),
            ]
        return argv

    async def _review_execution(
        self, raw, *, last_message_path, cli_version=None
    ):
        version = cli_version or self.probe_cli_version()
        if isinstance(raw, str):
            return await self._legacy_execution(raw, cli_version=version)
        if not isinstance(raw, ProcessOutput):
            raise TypeError("vendor runner must return text or ProcessOutput")
        if event_schema(self.vendor, version) is None:
            return await self._legacy_execution(raw, cli_version=version)
        last_message = ""
        final_truncated = False
        if last_message_path is not None:
            data, final_truncated = _read_file_bounded(
                last_message_path, 2 * 1024 * 1024
            )
            last_message = data.decode("utf-8", "replace")
        # Compatibility for fake/older binaries that ignore --json/-o and print the
        # final answer directly. Telemetry is unavailable, but review parsing survives.
        if not last_message and not raw.stdout.lstrip().startswith("{"):
            return await self._legacy_execution(raw, cli_version=version)
        parsed = parse_codex_jsonl(
            raw.stdout,
            last_message=last_message,
            cli_version="" if version == "unknown" else version,
            exit_code=raw.exit_code,
            stream_truncated=(
                raw.stdout_truncated or raw.stderr_truncated or final_truncated
            ),
        )
        return self._from_parsed(parsed, raw=raw)

    def _build_argv(self, hp):
        # argv는 docs/vendor-cli-contract.md(Task 0.5)에서 실증된 값.
        argv = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            hp.codex_sandbox,
        ]
        if hp.codex_model:  # 빈 값이면 codex 자체 기본 모델 사용
            argv += ["--model", hp.codex_model]
        # codex는 reasoning effort를 config로 받는다(-c). 유효값만 주입.
        if hp.codex_effort in _CODEX_EFFORTS:
            argv += ["-c", f"model_reasoning_effort={hp.codex_effort}"]
        # positional prompt 생략 시 codex exec는 stdin을 읽고 EOF 후 실행한다.
        return argv
