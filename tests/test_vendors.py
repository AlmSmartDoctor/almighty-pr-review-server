import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from server.review.harness import HarnessProfile
from server.review.vendors import (
    ClaudeAdapter,
    CodexAdapter,
    ProcessOutput,
    VendorProcessError,
    VendorReviewResult,
    VendorTimeout,
    VendorVerifyResult,
    _default_runner,
    public_cli_version,
    run_bounded_process,
    run_bounded_process_sync,
)


def fake_runner(stdout):
    calls = []

    async def run(args, env=None, cwd=None, timeout=None, input_text=None):
        calls.append({
            "args": args, "env": env, "cwd": cwd, "timeout": timeout,
            "input_text": input_text,
        })
        return stdout

    run.calls = calls
    return run


FAKE_OUT = (
    '분석 결과\n```json\n{"findings":[{"file":"a.py","line":2,'
    '"severity":"medium","category":"bug","claim":"c","rationale":"r",'
    '"confidence":0.6}]}\n```'
)


def test_claude_adapter_parses_findings(tmp_path):
    hp = HarnessProfile.load("default")
    runner = fake_runner(FAKE_OUT)
    adapter = ClaudeAdapter(runner=runner)
    fs = asyncio.run(
        adapter.review(
            prompt="리뷰해",
            workdir=tmp_path,
            harness=hp,
            runtime_dir=str(tmp_path / "rt"),
        )
    )
    assert fs[0].vendor == "claude"
    assert fs[0].file == "a.py"
    # read-only 격리 env 주입 + 전역 env 미상속(os.environ 통째 아님)
    assert "CLAUDE_CONFIG_DIR" in runner.calls[0]["env"]
    assert runner.calls[0]["timeout"] is not None
    args = runner.calls[0]["args"]
    assert args[:2] == ["claude", "-p"]
    assert "--allowedTools" in args
    assert "--model" in args


def test_codex_adapter_parses_findings(tmp_path):
    hp = HarnessProfile.load("default")
    runner = fake_runner(FAKE_OUT)
    adapter = CodexAdapter(runner=runner)
    fs = asyncio.run(
        adapter.review(
            prompt="리뷰해",
            workdir=tmp_path,
            harness=hp,
            runtime_dir=str(tmp_path / "rt"),
        )
    )
    assert fs[0].vendor == "codex"
    assert "CODEX_HOME" in runner.calls[0]["env"]
    args = runner.calls[0]["args"]
    assert args[:2] == ["codex", "exec"]
    assert "--skip-git-repo-check" in args
    assert "--sandbox" in args
    assert "--model" not in args  # codex_model="" → codex 자체 기본 모델


def test_codex_adapter_passes_model_when_set(tmp_path):
    hp = HarnessProfile.load("default")
    hp.codex_model = "gpt-5.4"
    runner = fake_runner(FAKE_OUT)
    adapter = CodexAdapter(runner=runner)
    asyncio.run(
        adapter.review(
            prompt="리뷰해",
            workdir=tmp_path,
            harness=hp,
            runtime_dir=str(tmp_path / "rt"),
        )
    )
    args = runner.calls[0]["args"]
    assert args[args.index("--model") + 1] == "gpt-5.4"
    assert "리뷰해" not in args
    assert "리뷰해" in runner.calls[0]["input_text"]


def test_adapter_complete_runs_isolated_freeform_task(tmp_path):
    hp = HarnessProfile.load("default")
    runner = fake_runner("ground truth json")
    adapter = ClaudeAdapter(runner=runner)
    out = asyncio.run(
        adapter.complete(
            prompt="레포를 분석해",
            system_prompt="Ground Truth만 작성",
            workdir=tmp_path,
            harness=hp,
            runtime_dir=str(tmp_path / "rt"),
        )
    )
    assert out == "ground truth json"
    assert "Ground Truth만 작성" in runner.calls[0]["input_text"]
    assert "레포를 분석해" in runner.calls[0]["input_text"]
    assert "레포를 분석해" not in runner.calls[0]["args"]
    assert "CLAUDE_CONFIG_DIR" in runner.calls[0]["env"]


def test_adapter_verify_parses_verdict(tmp_path):
    hp = HarnessProfile.load("default")
    runner = fake_runner('검토\n```json\n{"refuted":true,"rationale":"오탐"}\n```')
    adapter = ClaudeAdapter(runner=runner)
    v = asyncio.run(
        adapter.verify(
            prompt="검증해",
            workdir=tmp_path,
            harness=hp,
            runtime_dir=str(tmp_path / "rt"),
        )
    )
    assert v.refuted is True
    assert v.rationale == "오탐"
    assert "CLAUDE_CONFIG_DIR" in runner.calls[0]["env"]  # 격리 env 유지


def test_codex_adapter_passes_reasoning_effort(tmp_path):
    hp = HarnessProfile.load("default")
    hp.codex_effort = "high"
    runner = fake_runner(FAKE_OUT)
    asyncio.run(
        CodexAdapter(runner=runner).review(
            prompt="리뷰해", workdir=tmp_path, harness=hp, runtime_dir=str(tmp_path)
        )
    )
    args = runner.calls[0]["args"]
    assert "-c" in args
    assert "model_reasoning_effort=high" in args
    assert "리뷰해" not in args


def test_codex_adapter_omits_effort_when_unknown_value(tmp_path):
    hp = HarnessProfile.load("default")
    hp.codex_effort = "turbo"  # codex enum 밖 → 플래그 생략(400 방지)
    runner = fake_runner(FAKE_OUT)
    asyncio.run(
        CodexAdapter(runner=runner).review(
            prompt="리뷰해", workdir=tmp_path, harness=hp, runtime_dir=str(tmp_path)
        )
    )
    args = runner.calls[0]["args"]
    assert not any(a.startswith("model_reasoning_effort=") for a in args)


def test_claude_adapter_passes_effort(tmp_path):
    hp = HarnessProfile.load("default")
    hp.effort = "high"
    runner = fake_runner(FAKE_OUT)
    asyncio.run(
        ClaudeAdapter(runner=runner).review(
            prompt="리뷰해", workdir=tmp_path, harness=hp, runtime_dir=str(tmp_path)
        )
    )
    args = runner.calls[0]["args"]
    assert "--effort" in args
    assert args[args.index("--effort") + 1] == "high"
    assert "-c" not in args  # codex 전용 config 플래그는 claude에 없음


def test_claude_adapter_omits_effort_when_unknown_value(tmp_path):
    hp = HarnessProfile.load("default")
    hp.effort = "turbo"  # claude enum 밖 → 플래그 생략
    runner = fake_runner(FAKE_OUT)
    asyncio.run(
        ClaudeAdapter(runner=runner).review(
            prompt="리뷰해", workdir=tmp_path, harness=hp, runtime_dir=str(tmp_path)
        )
    )
    args = runner.calls[0]["args"]
    assert "--effort" not in args


def test_adapter_uses_per_vendor_system_prompt_with_shared_fallback(tmp_path):
    hp = HarnessProfile.load("default")
    hp.vendor_prompts = {"claude": "클로드 전용 지침"}
    # claude → 자기 오버라이드가 프롬프트 앞에 붙는다
    r1 = fake_runner(FAKE_OUT)
    asyncio.run(
        ClaudeAdapter(runner=r1).review(
            prompt="리뷰해", workdir=tmp_path, harness=hp, runtime_dir=str(tmp_path)
        )
    )
    claude_full = r1.calls[0]["input_text"]
    assert claude_full.startswith("클로드 전용 지침")
    # codex → 오버라이드 없음 → 공통 지침으로 폴백(stdin)
    r2 = fake_runner(FAKE_OUT)
    asyncio.run(
        CodexAdapter(runner=r2).review(
            prompt="리뷰해", workdir=tmp_path, harness=hp, runtime_dir=str(tmp_path)
        )
    )
    assert r2.calls[0]["input_text"].startswith(hp.system_prompt)
    assert hp.system_prompt not in r2.calls[0]["args"]


def test_default_runner_timeout_raises_vendor_timeout():
    with pytest.raises(VendorTimeout):
        asyncio.run(_default_runner(["sleep", "5"], timeout=0.2))


def test_public_cli_version_rejects_dynamic_wrapper_or_path_text():
    assert public_cli_version("codex", "codex-cli 0.144.5") == "codex-cli 0.144.5"
    assert public_cli_version("claude", "2.1.198 (Claude Code)") == "2.1.198 (Claude Code)"
    assert public_cli_version("codex", "/private/bin/codex 0.1") is None
    assert public_cli_version("claude", "SECRET_WRAPPER_TEXT") is None


def test_shared_process_runner_bounds_stdout_and_stderr_memory():
    output = asyncio.run(
        run_bounded_process(
            [
                sys.executable,
                "-c",
                "import sys; print('x'*10000); print('y'*10000, file=sys.stderr)",
            ],
            timeout=5,
            stream_limit=128,
        )
    )

    assert len(output.stdout.encode()) <= 128
    assert len(output.stderr.encode()) <= 128
    assert output.stdout_truncated is True
    assert output.stderr_truncated is True


def test_sync_control_plane_runner_is_also_bounded():
    output = run_bounded_process_sync(
        [sys.executable, "-c", "print('x'*10000)"],
        timeout=5,
        stream_limit=96,
    )

    assert len(output.stdout.encode()) <= 96
    assert output.stdout_truncated is True


def test_sync_control_plane_runner_enforces_wall_clock_timeout():
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded_process_sync(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=0.1,
            stream_limit=96,
        )


@pytest.mark.skipif(os.name != "posix", reason="process-group semantics are POSIX")
@pytest.mark.parametrize("runner_kind", ("async", "sync"))
def test_bounded_runner_timeout_kills_descendant_process(
    tmp_path, runner_kind
):
    marker = tmp_path / f"{runner_kind}.marker"
    child = (
        "import time; time.sleep(0.4); "
        f"open({str(marker)!r}, 'w').write('survived')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(5)"
    )
    if runner_kind == "async":
        with pytest.raises(VendorTimeout):
            asyncio.run(
                run_bounded_process(
                    [sys.executable, "-c", parent], timeout=0.1,
                    stream_limit=96,
                )
            )
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            run_bounded_process_sync(
                [sys.executable, "-c", parent], timeout=0.1,
                stream_limit=96,
            )
    time.sleep(0.6)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="process-group semantics are POSIX")
def test_bounded_runner_cancellation_kills_descendant_process(tmp_path):
    marker = tmp_path / "cancel.marker"
    child = (
        "import time; time.sleep(0.4); "
        f"open({str(marker)!r}, 'w').write('survived')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(5)"
    )

    async def cancel_run():
        task = asyncio.create_task(
            run_bounded_process(
                [sys.executable, "-c", parent], timeout=5,
                stream_limit=96,
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_run())
    time.sleep(0.6)
    assert not marker.exists()


def test_default_runner_nonzero_rc_returns_for_safe_adapter_classification():
    result = asyncio.run(
        _default_runner(["sh", "-c", "echo boom >&2; exit 3"], timeout=5)
    )
    assert result.exit_code == 3
    assert result.stderr.strip() == "boom"  # transient only; adapter never persists it


def test_codex_structured_review_returns_execution_telemetry(tmp_path, monkeypatch):
    hp = HarnessProfile.load("default")
    monkeypatch.setattr(
        "server.review.vendors._cli_version",
        lambda binary: "codex-cli 0.144.5",
    )

    async def runner(args, **kwargs):
        output_path = args[args.index("--output-last-message") + 1]
        from pathlib import Path
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(FAKE_OUT)
        events = "\n".join([
            '{"type":"thread.started","thread_id":"private"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"1","type":"command_execution","command":"cat secret"}}',
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":3,"output_tokens":4,"reasoning_output_tokens":1}}',
        ])
        return ProcessOutput(events, "", 0, 12)

    adapter = CodexAdapter(runner=runner)
    result = asyncio.run(
        adapter.review(
            prompt="리뷰해",
            workdir=tmp_path,
            harness=hp,
            runtime_dir=str(tmp_path / "rt"),
        )
    )

    assert isinstance(result, VendorReviewResult)
    assert result[0].file == "a.py"
    assert result.execution.telemetry["tool_calls"] == 1
    assert result.execution.telemetry["input_tokens"] == 10
    assert "command" not in result.execution.telemetry


def test_claude_unsupported_version_falls_back_to_legacy_text(tmp_path, monkeypatch):
    hp = HarnessProfile.load("default")
    monkeypatch.setattr(
        "server.review.vendors._cli_version", lambda binary: "2.2.0 (Claude Code)"
    )

    async def runner(args, **kwargs):
        assert "--output-format" not in args
        return ProcessOutput(FAKE_OUT, "", 0, 4)

    result = asyncio.run(
        ClaudeAdapter(runner=runner).review(
            prompt="리뷰해",
            workdir=tmp_path,
            harness=hp,
            runtime_dir=str(tmp_path / "rt"),
        )
    )

    assert result[0].file == "a.py"
    assert result.execution.telemetry["telemetry_status"] == "unavailable"


def test_codex_unsupported_version_falls_back_without_structured_flags(
    tmp_path, monkeypatch
):
    hp = HarnessProfile.load("default")
    monkeypatch.setattr(
        "server.review.vendors._cli_version", lambda binary: "codex-cli 0.100.0"
    )

    async def runner(args, **kwargs):
        for flag in (
            "--json", "--output-last-message", "--ignore-user-config", "--ignore-rules"
        ):
            assert flag not in args
        return ProcessOutput(FAKE_OUT, "", 0, 4)

    result = asyncio.run(
        CodexAdapter(runner=runner).review(
            prompt="리뷰해", workdir=tmp_path, harness=hp,
            runtime_dir=str(tmp_path / "rt"),
        )
    )

    assert result[0].file == "a.py"
    assert result.execution.telemetry["telemetry_status"] == "unavailable"


def test_legacy_stderr_truncation_is_a_safe_failure(tmp_path, monkeypatch):
    hp = HarnessProfile.load("default")
    monkeypatch.setattr(
        "server.review.vendors._cli_version", lambda binary: "2.2.0 (Claude Code)"
    )

    async def runner(args, **kwargs):
        return ProcessOutput(
            FAKE_OUT, "bounded", 0, 4, stderr_truncated=True
        )

    with pytest.raises(VendorProcessError) as caught:
        asyncio.run(
            ClaudeAdapter(runner=runner).review(
                prompt="리뷰해", workdir=tmp_path, harness=hp,
                runtime_dir=str(tmp_path / "rt"),
            )
        )
    assert caught.value.safe_error_code == "output_limit"


def test_codex_oversized_final_message_is_bounded_and_failed(
    tmp_path, monkeypatch
):
    hp = HarnessProfile.load("default")
    monkeypatch.setattr(
        "server.review.vendors._cli_version", lambda binary: "codex-cli 0.144.5"
    )

    async def runner(args, **kwargs):
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        events = '{"type":"turn.completed","usage":{"input_tokens":1}}'
        return ProcessOutput(events, "", 0, 4)

    with pytest.raises(VendorProcessError) as caught:
        asyncio.run(
            CodexAdapter(runner=runner).review(
                prompt="리뷰해", workdir=tmp_path, harness=hp,
                runtime_dir=str(tmp_path / "rt"),
            )
        )
    assert caught.value.safe_error_code == "output_limit"
    assert caught.value.execution.stream_truncated is True


def test_codex_legacy_fallback_keeps_invocation_bound_version(
    tmp_path, monkeypatch
):
    hp = HarnessProfile.load("default")
    versions = iter(("codex-cli 0.144.5", "codex-cli 0.145.0"))
    monkeypatch.setattr(
        "server.review.vendors._cli_version", lambda binary: next(versions)
    )

    async def runner(args, **kwargs):
        return ProcessOutput(FAKE_OUT, "", 0, 4)

    result = asyncio.run(
        CodexAdapter(runner=runner).review(
            prompt="리뷰해", workdir=tmp_path, harness=hp,
            runtime_dir=str(tmp_path / "rt"),
        )
    )
    assert result.execution.cli_version == "codex-cli 0.144.5"


def test_claude_unattested_exact_version_stays_legacy_for_verify(
    tmp_path, monkeypatch
):
    hp = HarnessProfile.load("default")
    monkeypatch.setattr(
        "server.review.vendors._cli_version",
        lambda binary: "2.1.198 (Claude Code)",
    )

    async def runner(args, **kwargs):
        assert "--output-format" not in args
        verdict = '```json\n{"refuted":false,"rationale":"valid"}\n```'
        return ProcessOutput(verdict, "", 0, 9)

    result = asyncio.run(
        ClaudeAdapter(runner=runner).verify(
            prompt="검증해",
            workdir=tmp_path,
            harness=hp,
            runtime_dir=str(tmp_path / "rt"),
        )
    )

    assert isinstance(result, VendorVerifyResult)
    assert result.refuted is False
    assert result.execution.telemetry["telemetry_status"] == "unavailable"
    assert result.execution.event_schema is None


def test_adapter_nonzero_raises_only_safe_error(tmp_path):
    hp = HarnessProfile.load("default")

    async def runner(*args, **kwargs):
        return ProcessOutput("", "provider leaked SECRET", 7, 3)

    with pytest.raises(VendorProcessError, match="process_exit") as caught:
        asyncio.run(
            ClaudeAdapter(runner=runner).review(
                prompt="리뷰해",
                workdir=tmp_path,
                harness=hp,
                runtime_dir=str(tmp_path / "rt"),
            )
        )
    assert "SECRET" not in str(caught.value)


def test_adapter_review_feeds_raw_sink_before_parse(tmp_path):
    # raw_sink는 파싱 전에 원문 stdout을 받아야 한다(파싱 실패 시에도 원문 보존).
    hp = HarnessProfile.load("default")
    adapter = ClaudeAdapter(runner=fake_runner("no json here"))
    captured = []
    with pytest.raises(Exception):
        asyncio.run(
            adapter.review(
                prompt="리뷰해",
                workdir=tmp_path,
                harness=hp,
                runtime_dir=str(tmp_path / "rt"),
                raw_sink=captured.append,
            )
        )
    assert captured == ["no json here"]
