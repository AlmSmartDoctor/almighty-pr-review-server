import subprocess

from server.github.gh import GitHubCliError
from server.repo_readiness import check_repo_readiness


class Gh:
    def preflight_repo(self, full_name):
        return {"full_name": full_name}


def _repo(**overrides):
    return {
        "id": 7,
        "full_name": "acme/api",
        "local_path": None,
        "enabled": 1,
        "harness_name": "default",
        "vendor_claude_on": 1,
        "vendor_codex_on": 0,
        **overrides,
    }


def test_repo_readiness_accepts_service_clone_and_one_installed_vendor(monkeypatch):
    monkeypatch.setattr("server.repo_readiness.list_harnesses", lambda: ["default"])

    out = check_repo_readiness(
        _repo(), Gh(), which=lambda command: f"/bin/{command}" if command == "claude" else None
    )

    assert out["ready"] is True
    assert out["checks"]["source"]["message"] == "서비스 전용 clone 사용"
    assert "claude" in out["checks"]["vendors"]["message"]


def test_repo_readiness_reports_all_actionable_failures(tmp_path, monkeypatch):
    class MissingGh:
        def preflight_repo(self, full_name):
            raise GitHubCliError(
                exit_code=1,
                message="GitHub repo not found",
                stderr="",
                command_kind="preflight_repo",
                http_status=404,
            )

    source = tmp_path / "not-git"
    source.mkdir()
    monkeypatch.setattr("server.repo_readiness.list_harnesses", lambda: [])

    out = check_repo_readiness(
        _repo(
            local_path=str(source),
            harness_name="missing",
            vendor_claude_on=1,
            vendor_codex_on=1,
        ),
        MissingGh(),
        which=lambda command: None,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", ""),
    )

    assert out["ready"] is False
    assert out["checks"]["github"] == {"ok": False, "message": "GitHub repo not found"}
    assert out["checks"]["source"]["ok"] is False
    assert out["checks"]["harness"]["ok"] is False
    assert out["checks"]["vendors"]["message"].endswith("claude, codex")


def test_repo_readiness_rejects_missing_local_directory(monkeypatch, tmp_path):
    monkeypatch.setattr("server.repo_readiness.list_harnesses", lambda: ["default"])

    out = check_repo_readiness(
        _repo(local_path=str(tmp_path / "missing")),
        Gh(),
        which=lambda command: f"/bin/{command}",
    )

    assert out["ready"] is False
    assert "존재하는 디렉터리" in out["checks"]["source"]["message"]


def test_repo_readiness_reports_disabled_repository(monkeypatch):
    monkeypatch.setattr("server.repo_readiness.list_harnesses", lambda: ["default"])

    out = check_repo_readiness(
        _repo(enabled=0), Gh(), which=lambda command: f"/bin/{command}"
    )

    assert out["ready"] is False
    assert out["checks"]["enabled"]["message"] == "레포가 비활성화되어 있습니다"


def test_repo_readiness_requires_an_enabled_vendor(monkeypatch):
    monkeypatch.setattr("server.repo_readiness.list_harnesses", lambda: ["default"])

    out = check_repo_readiness(
        _repo(vendor_claude_on=0, vendor_codex_on=0),
        Gh(),
        which=lambda command: f"/bin/{command}",
    )

    assert out["ready"] is False
    assert out["checks"]["vendors"]["message"] == "활성 vendor가 없습니다"
