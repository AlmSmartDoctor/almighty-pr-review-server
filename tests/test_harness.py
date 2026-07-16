import json
import os
import stat

import pytest

from server import config
from server.review.harness import (
    HarnessProfile,
    _link_codex_auth,
    _write_claude_credentials,
    set_vendor_prompt,
)


def _seed_harness(harness_dir, name="default", *, prompt="공통 지침"):
    d = harness_dir / name
    d.mkdir(parents=True)
    (d / "review-system-prompt.md").write_text(prompt)
    (d / "tools-allowlist.json").write_text(
        json.dumps({"claude_allowed_tools": ["Read"], "codex_sandbox": "read-only"})
    )
    (d / "config.json").write_text("{}")
    return d


def test_harness_loads_default(tmp_path, monkeypatch):
    hp = HarnessProfile.load("default")
    assert "코드 리뷰어" in hp.system_prompt
    assert hp.claude_allowed_tools == ["Read", "Grep", "Glob"]
    assert hp.codex_sandbox == "read-only"


def test_isolated_env_excludes_global_profile():
    hp = HarnessProfile.load("default")
    env = hp.isolated_env(runtime_dir="/tmp/rt")
    assert env["CLAUDE_CONFIG_DIR"].endswith("/claude")
    assert env["CODEX_HOME"].endswith("/codex")


def test_isolated_env_omits_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-xxx")
    monkeypatch.setenv("HOME", "/home/real")
    hp = HarnessProfile.load("default")
    env = hp.isolated_env(runtime_dir="/tmp/rt")
    assert env["HOME"] == "/tmp/rt"
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_write_claude_credentials_extracts_only_oauth(tmp_path):
    _write_claude_credentials(
        tmp_path,
        json.dumps({"claudeAiOauth": {"accessToken": "x"}, "mcpOAuth": {"srv": "y"}}),
    )
    dest = tmp_path / ".credentials.json"
    data = json.loads(dest.read_text())
    assert "claudeAiOauth" in data
    assert "mcpOAuth" not in data
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600


def test_write_claude_credentials_rejects_missing_field(tmp_path):
    with pytest.raises(RuntimeError):
        _write_claude_credentials(tmp_path, json.dumps({"mcpOAuth": {"srv": "y"}}))


def test_link_codex_auth_symlinks_source(tmp_path):
    source = tmp_path / "real-auth.json"
    source.write_text("{}")
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    _link_codex_auth(codex_dir, source)
    link = codex_dir / "auth.json"
    assert link.is_symlink()
    assert link.resolve() == source.resolve()


def test_link_codex_auth_noop_when_source_missing(tmp_path):
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    _link_codex_auth(codex_dir, tmp_path / "does-not-exist.json")
    assert not (codex_dir / "auth.json").exists()


def test_system_prompt_for_falls_back_to_shared_when_no_override():
    hp = HarnessProfile.load("default")
    assert hp.system_prompt_for("claude") == hp.system_prompt
    assert hp.system_prompt_for("codex") == hp.system_prompt


def test_load_reads_per_vendor_prompt_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_harness(config.HARNESS_DIR, prompt="공통 지침")
    set_vendor_prompt("default", "claude", "클로드 전용 지침")
    hp = HarnessProfile.load("default")
    assert hp.system_prompt_for("claude") == "클로드 전용 지침"
    assert hp.system_prompt_for("codex") == "공통 지침"  # 오버라이드 없음 → 폴백
    assert hp.vendor_prompts == {"claude": "클로드 전용 지침"}


def test_set_vendor_prompt_empty_reverts_to_shared(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_harness(config.HARNESS_DIR, prompt="공통 지침")
    set_vendor_prompt("default", "codex", "코덱스 전용")
    assert HarnessProfile.load("default").system_prompt_for("codex") == "코덱스 전용"
    set_vendor_prompt("default", "codex", "")  # 비우면 오버라이드 제거
    hp = HarnessProfile.load("default")
    assert hp.vendor_prompts == {}
    assert hp.system_prompt_for("codex") == "공통 지침"


def test_set_vendor_prompt_rejects_unknown_vendor(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_harness(config.HARNESS_DIR)
    with pytest.raises(ValueError):
        set_vendor_prompt("default", "gpt", "x")
