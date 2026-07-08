import asyncio
import os
import stat

from server.review.harness import HarnessProfile
from server.review.vendors import ClaudeAdapter


def _fake_bin(dir_, name, stdout):
    p = dir_ / name
    p.write_text(f"#!/usr/bin/env bash\ncat >/dev/null\ncat <<'EOF'\n{stdout}\nEOF\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


FAKE_OUT = (
    'ok\n```json\n{"findings":[{"file":"a.py","line":1,'
    '"severity":"low","category":"style","claim":"c","rationale":"r",'
    '"confidence":0.3}]}\n```'
)


def test_claude_real_subprocess(tmp_path, monkeypatch):
    _fake_bin(tmp_path, "claude", FAKE_OUT)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    hp = HarnessProfile.load("default")
    fs = asyncio.run(
        ClaudeAdapter().review(
            prompt="리뷰",
            workdir=tmp_path,
            harness=hp,
            runtime_dir=str(tmp_path / "rt"),
        )
    )
    assert fs[0].file == "a.py"  # stdin 닫힘 + 종료코드0 + 파싱까지 실경로 검증


def test_codex_real_subprocess(tmp_path, monkeypatch):  # ★개정: codex 경로도
    from server.review.vendors import CodexAdapter

    _fake_bin(tmp_path, "codex", FAKE_OUT)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    hp = HarnessProfile.load("default")
    fs = asyncio.run(
        CodexAdapter().review(
            prompt="리뷰",
            workdir=tmp_path,
            harness=hp,
            runtime_dir=str(tmp_path / "rt"),
        )
    )
    assert fs[0].vendor == "codex" and fs[0].file == "a.py"
