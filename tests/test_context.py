from server.context.base import ContextRequest, ContextResult
from server.context.composite import CompositeContextProvider


def _req(**kw):
    b = dict(repo="acme/api", pr_number=7)
    b.update(kw)
    return ContextRequest(**b)


class _Fake:
    def __init__(self, name="fake", text=None, exc=None):
        self.name, self._t, self._e = name, text, exc

    def fetch(self, req):
        if self._e:
            raise self._e
        return ContextResult(
            provider=self.name, status="ok" if self._t else "empty", text=self._t or ""
        )


def test_composite_empty():
    c = CompositeContextProvider([])
    assert c.gather(req=_req()) == "" and c.results == []


def test_composite_renders_and_records():
    c = CompositeContextProvider([_Fake(text="hello")])
    out = c.gather(req=_req())
    assert "hello" in out and [r.status for r in c.results] == ["ok"]


def test_composite_degrades_and_redacts():
    c = CompositeContextProvider(
        [_Fake(exc=RuntimeError("boom SECRETXYZ"))],
        redactor=lambda s: s.replace("SECRETXYZ", "[redacted]"),
    )
    assert c.gather(req=_req()) == ""  # B-INV-4 degrade
    assert c.results[0].status == "error" and "SECRETXYZ" not in (
        c.results[0].error or ""
    )


def test_redact_secrets_masks_configured_token(monkeypatch):
    from server import config
    from server.context.base import redact_secrets

    monkeypatch.setattr(config, "JIRA_API_TOKEN", "tok-SEKRET")
    monkeypatch.setattr(config, "JIRA_EMAIL", "")
    assert redact_secrets("auth=tok-SEKRET done") == "auth=[redacted] done"


def test_redact_secrets_ignores_empty_secrets(monkeypatch):
    from server import config
    from server.context.base import redact_secrets

    monkeypatch.setattr(config, "JIRA_API_TOKEN", "")
    monkeypatch.setattr(config, "JIRA_EMAIL", "")
    assert redact_secrets("nothing to mask") == "nothing to mask"


def test_effective_prefers_repo_then_settings_then_off():
    from server.context.registry import _effective

    assert _effective({"k": 1}, {"k": 0}, "k") == 1  # repo override
    assert _effective({"k": None}, {"k": 1}, "k") == 1  # NULL → inherit settings
    assert _effective({}, {"k": 1}, "k") == 1  # repo missing key → settings
    assert _effective({}, {}, "k") == 0  # neither → off


def test_effective_with_real_rows(db):
    from server.repos import repo_repo, settings_repo
    from server.context.registry import _effective

    rid = repo_repo.add(db, full_name="acme/api")
    settings_repo.update(db, context_static_on=1)
    repo = repo_repo.get(db, rid)  # per-repo NULL → inherit
    settings = settings_repo.get(db)
    assert _effective(repo, settings, "context_static_on") == 1
    repo_repo.update(db, rid, context_static_on=0)  # explicit override off
    assert _effective(repo_repo.get(db, rid), settings, "context_static_on") == 0


def test_static_reads_within_root(tmp_path):
    from server.context.static_provider import StaticContextProvider

    (tmp_path / "c.md").write_text("설계 노트 X")
    r = StaticContextProvider(path=str(tmp_path / "c.md"), root=str(tmp_path)).fetch(
        _req()
    )
    assert r.status == "ok" and "설계 노트" in r.text


def test_static_rejects_outside_root(tmp_path):
    from server.context.static_provider import StaticContextProvider

    r = StaticContextProvider(path="/etc/passwd", root=str(tmp_path)).fetch(_req())
    assert r.status in ("error", "empty") and r.text == ""  # B-INV-9: exfil 차단


def test_static_rejects_dotdot_escape(tmp_path):
    from server.context.static_provider import StaticContextProvider

    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    root = tmp_path / "repo"
    root.mkdir()
    # repo 하위처럼 보이지만 ../secret.txt 로 탈출
    r = StaticContextProvider(
        path=str(root / ".." / "secret.txt"), root=str(root)
    ).fetch(_req())
    assert r.text == "" and "SECRET" not in r.text  # 탈출 거부


def test_static_degrades_when_missing(tmp_path):
    from server.context.static_provider import StaticContextProvider

    r = StaticContextProvider(path=str(tmp_path / "none.md"), root=str(tmp_path)).fetch(
        _req()
    )
    assert r.text == ""


def test_static_rejects_symlink_escape(tmp_path):
    from server.context.static_provider import StaticContextProvider

    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    (root / "link").symlink_to(tmp_path / "secret.txt")  # root 안에 있지만 밖을 가리킴
    r = StaticContextProvider(path=str(root / "link"), root=str(root)).fetch(_req())
    assert (
        r.status == "error" and "SECRET" not in r.text
    )  # realpath가 심볼릭 링크 해석 → 차단
