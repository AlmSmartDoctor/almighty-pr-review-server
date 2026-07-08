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


def test_default_runner_timeout_raises_vendor_timeout():
    with pytest.raises(VendorTimeout):
        asyncio.run(_default_runner(["sleep", "5"], timeout=0.2))


def test_default_runner_nonzero_rc_surfaces_stderr():
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(_default_runner(["sh", "-c", "echo boom >&2; exit 3"], timeout=5))
