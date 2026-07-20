import asyncio

import pytest

from server.review.harness import HarnessProfile
from server.review.vendors import (
    ClaudeAdapter,
    CodexAdapter,
    VendorTimeout,
    _default_runner,
)


def fake_runner(stdout):
    calls = []

    async def run(args, env=None, cwd=None, timeout=None):  # ★개정: async
        calls.append({"args": args, "env": env, "cwd": cwd, "timeout": timeout})
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
    assert args[-1] != "gpt-5.4"  # prompt는 positional로 항상 마지막


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
    assert "Ground Truth만 작성" in runner.calls[0]["args"][2]
    assert "레포를 분석해" in runner.calls[0]["args"][2]
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
    assert args[-1] != "model_reasoning_effort=high"  # prompt는 positional 마지막


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
    claude_full = r1.calls[0]["args"][r1.calls[0]["args"].index("-p") + 1]
    assert claude_full.startswith("클로드 전용 지침")
    # codex → 오버라이드 없음 → 공통 지침으로 폴백(positional 마지막 인자)
    r2 = fake_runner(FAKE_OUT)
    asyncio.run(
        CodexAdapter(runner=r2).review(
            prompt="리뷰해", workdir=tmp_path, harness=hp, runtime_dir=str(tmp_path)
        )
    )
    assert r2.calls[0]["args"][-1].startswith(hp.system_prompt)


def test_default_runner_timeout_raises_vendor_timeout():
    with pytest.raises(VendorTimeout):
        asyncio.run(_default_runner(["sleep", "5"], timeout=0.2))


def test_default_runner_nonzero_rc_surfaces_stderr():
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(_default_runner(["sh", "-c", "echo boom >&2; exit 3"], timeout=5))


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
