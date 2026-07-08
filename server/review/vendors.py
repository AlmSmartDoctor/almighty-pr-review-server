import asyncio
from pathlib import Path

from server.models import Finding
from server.review.findings_schema import PROMPT_SCHEMA_HINT, parse_findings
from server.review.harness import HarnessProfile

VENDOR_TIMEOUT_SEC = 600  # 벤더별 상한(rate-limit/hang 방어)


class VendorTimeout(RuntimeError):
    pass


async def _default_runner(args, env=None, cwd=None, timeout=None) -> str:
    """async subprocess — 이벤트루프 블록 금지(★개정). stdin은 반드시 닫음.

    (Task 0.5에서 확인: codex는 positional prompt를 줘도 stdin을 추가로 읽어
     닫지 않으면 무한 대기함 → stdin=DEVNULL 필수.)
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=env,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise VendorTimeout(f"vendor timeout after {timeout}s") from e
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace")[:500])
    return out.decode(errors="replace")


class _BaseAdapter:
    vendor = ""

    def __init__(self, runner=_default_runner, timeout=VENDOR_TIMEOUT_SEC):
        self._run = runner
        self._timeout = timeout

    def _build_argv(self, prompt: str, hp: HarnessProfile) -> list[str]:
        raise NotImplementedError

    async def review(
        self, *, prompt: str, workdir: Path, harness: HarnessProfile, runtime_dir: str
    ) -> list[Finding]:
        full = f"{harness.system_prompt}\n\n{prompt}\n\n{PROMPT_SCHEMA_HINT}"
        env = harness.isolated_env(runtime_dir=runtime_dir)  # ★개정: allowlist env
        out = await self._run(
            self._build_argv(full, harness),
            env=env,
            cwd=str(workdir),
            timeout=self._timeout,
        )
        return parse_findings(out, vendor=self.vendor)


class ClaudeAdapter(_BaseAdapter):
    vendor = "claude"

    def _build_argv(self, prompt, hp):
        # argv는 docs/vendor-cli-contract.md(Task 0.5)에서 실증된 값.
        tools = ",".join(hp.claude_allowed_tools)
        return [
            "claude",
            "-p",
            prompt,
            "--allowedTools",
            tools,
            "--model",
            hp.model,
        ]


class CodexAdapter(_BaseAdapter):
    vendor = "codex"

    def _build_argv(self, prompt, hp):
        # argv는 docs/vendor-cli-contract.md(Task 0.5)에서 실증된 값.
        return [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            hp.codex_sandbox,
            prompt,
        ]
