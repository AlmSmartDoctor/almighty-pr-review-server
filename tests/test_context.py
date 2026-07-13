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
