import asyncio
import json
import os
import stat
import sys

import pytest

from server import config
from server.review import harness as harness_module
from server.review.harness import (
    HarnessProfile,
    RuntimeCredentialError,
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
    assert "허용되는 실제 추가 라인" in hp.system_prompt
    assert "같은 파일·같은 범위를 반복해서 읽" in hp.system_prompt
    assert "후보 없이 레포 전체를 넓게 검색하지 않는다" in hp.system_prompt
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


def test_prepare_runtime_materializes_only_codex_auth(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        harness_module,
        "_link_codex_auth",
        lambda destination, source: calls.append((destination, source)),
    )
    monkeypatch.setattr(
        harness_module,
        "_read_claude_keychain",
        lambda: pytest.fail("claude keychain must not be read"),
    )

    HarnessProfile.load("default").prepare_runtime(
        runtime_dir=str(tmp_path), vendor="codex"
    )

    assert len(calls) == 1
    assert calls[0][0] == tmp_path / "codex"
    assert not (tmp_path / "claude").exists()


def test_prepare_runtime_materializes_only_claude_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(
        harness_module,
        "_link_codex_auth",
        lambda *args: pytest.fail("codex auth must not be linked"),
    )
    monkeypatch.setattr(
        harness_module,
        "_read_claude_keychain",
        lambda: json.dumps({"claudeAiOauth": {"accessToken": "x"}}),
    )

    HarnessProfile.load("default").prepare_runtime(
        runtime_dir=str(tmp_path), vendor="claude"
    )

    assert (tmp_path / "claude" / ".credentials.json").exists()
    assert not (tmp_path / "codex").exists()


def test_prepare_runtime_rejects_unknown_vendor(tmp_path):
    with pytest.raises(ValueError, match="invalid vendor"):
        HarnessProfile.load("default").prepare_runtime(
            runtime_dir=str(tmp_path), vendor="other"
        )


def test_runtime_credentials_cleanup_runs_on_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        harness_module,
        "_read_claude_keychain",
        lambda: json.dumps({"claudeAiOauth": {"accessToken": "x"}}),
    )
    credential = tmp_path / "claude" / ".credentials.json"

    with pytest.raises(asyncio.CancelledError):
        with HarnessProfile.load("default").runtime_credentials(
            runtime_dir=str(tmp_path), vendor="claude"
        ):
            assert credential.exists()
            raise asyncio.CancelledError

    assert not credential.exists()


def test_runtime_cleanup_failure_does_not_mask_cancellation(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        harness_module,
        "_read_claude_keychain",
        lambda: json.dumps({"claudeAiOauth": {"accessToken": "x"}}),
    )
    credential = tmp_path / "claude" / ".credentials.json"
    real_unlink = harness_module.Path.unlink

    def fail_credential_unlink(path, *args, **kwargs):
        if path == credential:
            raise OSError("simulated cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(harness_module.Path, "unlink", fail_credential_unlink)
    with pytest.raises(asyncio.CancelledError) as caught:
        with HarnessProfile.load("default").runtime_credentials(
            runtime_dir=str(tmp_path), vendor="claude"
        ):
            raise asyncio.CancelledError

    assert "runtime_cleanup_failed" in getattr(caught.value, "__notes__", [])
    assert "runtime_cleanup_failed" in capsys.readouterr().out
    real_unlink(credential)


@pytest.mark.parametrize("mode", ("oversized", "timeout"))
def test_claude_keychain_reader_is_bounded(tmp_path, monkeypatch, mode):
    security = tmp_path / "security"
    body = (
        "print('x' * 70000)"
        if mode == "oversized"
        else "import time; time.sleep(5); print('{}')"
    )
    security.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    security.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    if mode == "timeout":
        monkeypatch.setattr(harness_module, "_KEYCHAIN_TIMEOUT_SEC", 0.1)

    with pytest.raises(RuntimeError, match="claude keychain read failed"):
        harness_module._read_claude_keychain()


def test_runtime_credentials_cleanup_runs_after_partial_setup_failure(
    tmp_path, monkeypatch
):
    credential = tmp_path / "claude" / ".credentials.json"
    monkeypatch.setattr(harness_module, "_read_claude_keychain", lambda: "{}")

    def partial_write(directory, payload):
        credential.write_text("partial")
        raise OSError("simulated setup failure")

    monkeypatch.setattr(harness_module, "_write_claude_credentials", partial_write)

    with pytest.raises(RuntimeCredentialError) as caught:
        with HarnessProfile.load("default").runtime_credentials(
            runtime_dir=str(tmp_path), vendor="claude"
        ):
            pass

    assert caught.value.safe_error_code == "runtime_setup_failed"
    assert not credential.exists()


def test_runtime_credentials_cleanup_residue_is_not_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        harness_module,
        "_read_claude_keychain",
        lambda: json.dumps({"claudeAiOauth": {"accessToken": "x"}}),
    )
    credential = tmp_path / "claude" / ".credentials.json"
    real_unlink = harness_module.Path.unlink

    def fail_credential_unlink(path, *args, **kwargs):
        if path == credential:
            raise OSError("simulated cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(harness_module.Path, "unlink", fail_credential_unlink)
    with pytest.raises(RuntimeCredentialError) as caught:
        with HarnessProfile.load("default").runtime_credentials(
            runtime_dir=str(tmp_path), vendor="claude"
        ):
            pass

    assert caught.value.safe_error_code == "runtime_cleanup_failed"
    real_unlink(credential)


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
