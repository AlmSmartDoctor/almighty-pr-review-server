import asyncio
from pathlib import Path

from server.models import Finding
from server.review.findings_schema import PROMPT_SCHEMA_HINT, parse_findings
from server.review.harness import HarnessProfile

VENDOR_TIMEOUT_SEC = 600  # 벤더별 상한(rate-limit/hang 방어)

# codex reasoning effort 유효값(codex-cli 0.144.1 API가 실증한 enum; docs/vendor-cli-contract.md).
# 이 집합 밖 값이면 codex가 400으로 리뷰 전체를 깨므로, 모르는 값은 플래그 생략(codex 기본).
_CODEX_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
# claude --effort 유효값(명시적으로 유효값만 전달; 그 밖의 값은 플래그 생략).
_CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


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

    async def verify(
        self, *, prompt: str, workdir: Path, harness: HarnessProfile, runtime_dir: str
    ):
        from server.review.verify import VERIFY_SCHEMA_HINT, parse_verdict

        full = f"{harness.system_prompt}\n\n{prompt}\n\n{VERIFY_SCHEMA_HINT}"
        env = harness.isolated_env(runtime_dir=runtime_dir)
        out = await self._run(
            self._build_argv(full, harness),
            env=env,
            cwd=str(workdir),
            timeout=self._timeout,
        )
        return parse_verdict(out)


class ClaudeAdapter(_BaseAdapter):
    vendor = "claude"

    def _build_argv(self, prompt, hp):
        # argv는 docs/vendor-cli-contract.md(Task 0.5)에서 실증된 값.
        tools = ",".join(hp.claude_allowed_tools)
        argv = [
            "claude",
            "-p",
            prompt,
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

    def _build_argv(self, prompt, hp):
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
        argv.append(prompt)  # prompt는 positional → 항상 마지막
        return argv
