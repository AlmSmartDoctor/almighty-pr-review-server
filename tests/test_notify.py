from server import config, notify
from server.notify import _esc, notify_review_done


def test_notify_disabled_by_config(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_ON_DONE", False)
    monkeypatch.setattr(
        notify.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    notify_review_done(repo_full="a/b", pr_number=1, status="done", findings=3)


def test_notify_builds_osascript_message(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_ON_DONE", True)
    calls = []
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    notify_review_done(repo_full="acme/api", pr_number=7, status="done", findings=2)
    assert len(calls) == 1 and calls[0][0] == "osascript"
    script = calls[0][2]
    assert "acme/api #7" in script and "finding 2건" in script


def test_notify_failure_message_omits_findings(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_ON_DONE", True)
    calls = []
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    notify_review_done(repo_full="acme/api", pr_number=7, status="failed", findings=0)
    assert "리뷰 실패" in calls[0][2] and "finding" not in calls[0][2]


def test_notify_swallows_subprocess_errors(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_ON_DONE", True)

    def boom(*a, **kw):
        raise FileNotFoundError("osascript missing")

    monkeypatch.setattr(notify.subprocess, "run", boom)
    notify_review_done(repo_full="a/b", pr_number=1, status="done", findings=0)


def test_esc_neutralizes_applescript_quotes():
    assert _esc('say "hi" \\ end') == "say 'hi' \\\\ end"
