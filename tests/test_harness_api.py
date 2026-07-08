import json

from fastapi.testclient import TestClient

from server import config
from server.api import app


def _seed_default_harness(harness_dir):
    d = harness_dir / "default"
    d.mkdir(parents=True)
    (d / "review-system-prompt.md").write_text("원본 리뷰 지침")
    (d / "tools-allowlist.json").write_text(
        json.dumps(
            {
                "claude_allowed_tools": ["Read", "Grep", "Glob"],
                "codex_sandbox": "read-only",
                "mcp": "none",
            }
        )
    )
    (d / "config.json").write_text(
        json.dumps({"model": "sonnet", "effort": "medium", "prescreen_model": "haiku"})
    )


def test_get_and_update_harness_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_default_harness(config.HARNESS_DIR)
    client = TestClient(app)
    got = client.get("/api/harness/default").json()
    assert "system_prompt" in got
    r = client.put("/api/harness/default", json={"system_prompt": "새 리뷰 지침"})
    assert r.status_code == 200
    assert client.get("/api/harness/default").json()["system_prompt"] == "새 리뷰 지침"


def test_put_none_leaves_prompt_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_default_harness(config.HARNESS_DIR)
    client = TestClient(app)
    r = client.put("/api/harness/default", json={})
    assert r.status_code == 200
    assert (
        client.get("/api/harness/default").json()["system_prompt"] == "원본 리뷰 지침"
    )
