from fastapi.testclient import TestClient

from server.api import app, get_conn, get_gh
from server.db import connect, init_schema
from server.github.gh import GitHubCliError
from server.health import deep_health


class FakeGh:
    def __init__(self, login="tester", error=None):
        self._login = login
        self._error = error

    def preflight_user(self):
        if self._error:
            raise self._error
        return {"login": self._login}


def _which_all(name):
    return f"/usr/local/bin/{name}"


def test_deep_health_all_ok(db):
    h = deep_health(db, FakeGh(), which=_which_all)
    assert h["ok"] is True
    assert h["gh"] == {
        "installed": True,
        "authenticated": True,
        "login": "tester",
        "error": None,
    }
    assert h["claude"]["installed"] and h["codex"]["installed"]
    assert h["db"]["ok"] is True


def test_deep_health_gh_not_installed(db):
    h = deep_health(db, FakeGh(), which=lambda name: None)
    assert h["ok"] is False
    assert h["gh"]["installed"] is False
    assert h["gh"]["authenticated"] is False
    assert h["gh"]["error"]


def test_deep_health_gh_unauthenticated(db):
    err = GitHubCliError(
        exit_code=1,
        message="HTTP 401: Bad credentials",
        stderr="",
        command_kind="preflight_user",
        http_status=401,
    )
    h = deep_health(db, FakeGh(error=err), which=_which_all)
    assert h["ok"] is False
    assert h["gh"]["installed"] is True
    assert h["gh"]["authenticated"] is False
    assert "401" in h["gh"]["error"]


def test_deep_health_no_vendor_cli(db):
    h = deep_health(
        db, FakeGh(), which=lambda name: _which_all(name) if name == "gh" else None
    )
    assert h["ok"] is False
    assert h["claude"]["installed"] is False
    assert h["codex"]["installed"] is False


def test_deep_health_one_vendor_suffices(db):
    h = deep_health(
        db, FakeGh(), which=lambda name: None if name == "codex" else _which_all(name)
    )
    assert h["ok"] is True
    assert h["codex"]["installed"] is False


def test_deep_health_endpoint(tmp_path, monkeypatch):
    import server.health

    conn = connect(tmp_path / "h.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_gh] = lambda: FakeGh()
    monkeypatch.setattr(server.health.shutil, "which", _which_all)
    try:
        resp = TestClient(app).get("/api/health/deep")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True and body["gh"]["login"] == "tester"
    finally:
        app.dependency_overrides.clear()
        conn.close()
