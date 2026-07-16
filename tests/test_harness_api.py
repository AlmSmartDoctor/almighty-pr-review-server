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
    (d / "config.json").write_text(json.dumps({"prescreen_model": "haiku"}))


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


def test_list_harnesses(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_default_harness(config.HARNESS_DIR)
    client = TestClient(app)
    assert client.get("/api/harness").json()["harnesses"] == ["default"]


def test_put_creates_new_harness_scaffolded_from_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_default_harness(config.HARNESS_DIR)
    client = TestClient(app)

    r = client.put("/api/harness/security-focus", json={"system_prompt": "보안 집중"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "security-focus"
    assert body["system_prompt"] == "보안 집중"
    # config/tools는 default에서 복사됨
    assert body["claude_allowed_tools"] == ["Read", "Grep", "Glob"]
    assert body["codex_sandbox"] == "read-only"
    assert sorted(client.get("/api/harness").json()["harnesses"]) == [
        "default",
        "security-focus",
    ]


def test_put_create_without_prompt_inherits_default_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_default_harness(config.HARNESS_DIR)
    client = TestClient(app)
    r = client.put("/api/harness/perf_focus", json={})
    assert r.status_code == 200
    assert r.json()["system_prompt"] == "원본 리뷰 지침"


def test_invalid_harness_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_default_harness(config.HARNESS_DIR)
    client = TestClient(app)
    # 대문자/점 등 무효명은 400, 'default' 외 아무 디렉토리도 생성하지 않음
    assert (
        client.put("/api/harness/Bad.Name", json={"system_prompt": "x"}).status_code
        == 400
    )
    assert (
        client.put("/api/harness/UPPER", json={"system_prompt": "x"}).status_code == 400
    )
    assert client.get("/api/harness/Bad.Name").status_code == 400
    assert list(config.HARNESS_DIR.iterdir()) == [config.HARNESS_DIR / "default"]


def test_validate_harness_name_blocks_traversal():
    import pytest

    from server.review.harness import validate_harness_name

    for good in ("default", "security-focus", "perf_focus", "v2"):
        assert validate_harness_name(good) == good
    for bad in (
        "../etc",
        "a/b",
        "..",
        ".",
        "",
        "Upper",
        "dot.name",
        "sp ace",
        "x" * 65,
    ):
        with pytest.raises(ValueError):
            validate_harness_name(bad)


def test_get_missing_harness_404(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_default_harness(config.HARNESS_DIR)
    client = TestClient(app)
    assert client.get("/api/harness/nonexistent").status_code == 404


def test_get_harness_includes_empty_vendor_prompts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_default_harness(config.HARNESS_DIR)
    client = TestClient(app)
    assert client.get("/api/harness/default").json()["vendor_prompts"] == {}


def test_put_and_get_per_vendor_prompt_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_default_harness(config.HARNESS_DIR)
    client = TestClient(app)
    r = client.put(
        "/api/harness/default", json={"vendor_prompts": {"claude": "클로드 전용"}}
    )
    assert r.status_code == 200
    got = client.get("/api/harness/default").json()
    assert got["vendor_prompts"] == {"claude": "클로드 전용"}
    assert got["system_prompt"] == "원본 리뷰 지침"  # 공통 지침은 그대로


def test_put_empty_vendor_prompt_reverts_to_shared(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_default_harness(config.HARNESS_DIR)
    client = TestClient(app)
    client.put(
        "/api/harness/default", json={"vendor_prompts": {"codex": "코덱스 전용"}}
    )
    client.put("/api/harness/default", json={"vendor_prompts": {"codex": ""}})
    assert client.get("/api/harness/default").json()["vendor_prompts"] == {}


def test_put_invalid_vendor_key_rejected_400(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DIR", tmp_path / "harness")
    _seed_default_harness(config.HARNESS_DIR)
    client = TestClient(app)
    r = client.put("/api/harness/default", json={"vendor_prompts": {"gpt": "x"}})
    assert r.status_code == 400
