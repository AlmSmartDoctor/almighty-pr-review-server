import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from server import config
from server.api import app, get_conn
from server.db import connect, init_schema
from server.repos import feedback_repo, pr_repo, repo_repo, review_repo
from server.slack.webhook import is_fresh, parse_event, verdict_for, verify_signature

_SECRET = "slacksign"
_FRESH = object()  # _post 기본값 — 호출 시점 현재 타임스탬프(freshness 통과)로 치환


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _client(tmp_path):
    conn = connect(tmp_path / "s.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    return TestClient(app), conn


def _sign(ts, body):
    base = b"v0:" + ts.encode() + b":" + body
    return "v0=" + hmac.new(_SECRET.encode(), base, hashlib.sha256).hexdigest()


def _post(client, body, *, ts=_FRESH, sig="auto"):
    if ts is _FRESH:
        ts = str(int(time.time()))
    headers = {}
    if ts is not None:
        headers["X-Slack-Request-Timestamp"] = ts
    if sig == "auto":
        sig = _sign(ts, body)
    if sig is not None:
        headers["X-Slack-Signature"] = sig
    return client.post("/api/webhooks/slack", content=body, headers=headers)


def _seed_run(conn, *, full_name="acme/api", channel="C1", ts="111.1"):
    rid = repo_repo.add(conn, full_name=full_name)
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=5,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    feedback_repo.record_slack_post(conn, run_id=run_id, channel=channel, ts=ts)
    return run_id


# ---- pure helpers ---------------------------------------------------------


def test_verdict_mapping_and_skin_tone_normalization():
    assert verdict_for("+1") == "positive"
    assert verdict_for("thumbsup::skin-tone-3") == "positive"
    assert verdict_for("-1") == "negative"
    assert verdict_for("eyes") is None
    assert verdict_for("") is None


def test_verify_signature_valid_and_invalid():
    body = b'{"ok":1}'
    assert verify_signature(_SECRET, "123", body, _sign("123", body)) is True
    assert verify_signature(_SECRET, "123", body, "v0=deadbeef") is False
    assert verify_signature("", "123", body, _sign("123", body)) is False
    assert verify_signature(_SECRET, None, body, _sign("123", body)) is False
    assert verify_signature(_SECRET, "123", body, None) is False


def test_is_fresh_window_and_non_numeric():
    assert is_fresh("1000", now=1000) is True
    assert is_fresh("1000", now=1000 + 299) is True
    assert is_fresh("1000", now=1000 + 301) is False
    assert is_fresh("1000", now=1000 - 301) is False
    assert is_fresh(None, now=1000) is False
    assert is_fresh("not-a-number", now=1000) is False


def test_parse_event_url_verification_and_reaction():
    ch = parse_event(
        json.dumps({"type": "url_verification", "challenge": "z"}).encode()
    )
    assert ch == {"type": "url_verification", "challenge": "z"}
    ev = parse_event(
        json.dumps(
            {
                "type": "event_callback",
                "event": {
                    "type": "reaction_added",
                    "reaction": "+1",
                    "user": "U1",
                    "item": {"channel": "C1", "ts": "111.1"},
                },
            }
        ).encode()
    )
    assert ev["type"] == "reaction" and ev["action"] == "added"
    assert ev["channel"] == "C1" and ev["ts"] == "111.1"
    assert parse_event(b"not json") is None
    assert parse_event(json.dumps({"type": "event_callback"}).encode()) is None


# ---- endpoint -------------------------------------------------------------


def test_missing_secret_returns_503(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SLACK_SIGNING_SECRET", "")
    client, _ = _client(tmp_path)
    assert _post(client, b"{}", sig=None).status_code == 503


def test_bad_signature_returns_401(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SLACK_SIGNING_SECRET", _SECRET)
    client, _ = _client(tmp_path)
    assert _post(client, b"{}", sig="v0=wrong").status_code == 401


def test_nonascii_signature_is_401_not_500(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SLACK_SIGNING_SECRET", _SECRET)
    client, _ = _client(tmp_path)
    r = client.post(
        "/api/webhooks/slack",
        content=b"{}",
        headers={
            "X-Slack-Request-Timestamp": "123",
            "X-Slack-Signature": b"v0=\xff",
        },
    )
    assert r.status_code == 401


def test_stale_timestamp_returns_401(tmp_path, monkeypatch):
    # 서명은 유효(그 오래된 ts로 서명)해도 replay 윈도우 밖이면 거부.
    monkeypatch.setattr(config, "SLACK_SIGNING_SECRET", _SECRET)
    client, _ = _client(tmp_path)
    assert _post(client, b"{}", ts="100").status_code == 401


def test_url_verification_echoes_challenge(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SLACK_SIGNING_SECRET", _SECRET)
    client, _ = _client(tmp_path)
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    r = _post(client, body)
    assert r.status_code == 200 and r.json() == {"challenge": "abc123"}


def test_reaction_added_records_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SLACK_SIGNING_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    _seed_run(conn)
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "reaction_added",
                "reaction": "+1",
                "user": "U1",
                "item": {"channel": "C1", "ts": "111.1"},
            },
        }
    ).encode()
    r = _post(client, body)
    assert r.status_code == 200 and r.json()["verdict"] == "positive"
    rows = conn.execute("SELECT * FROM feedback_signal").fetchall()
    assert len(rows) == 1 and rows[0]["verdict"] == "positive"


def test_reaction_removed_deletes_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SLACK_SIGNING_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    run_id = _seed_run(conn)
    feedback_repo.add_reaction(
        conn, run_id=run_id, slack_user="U1", reaction="+1", verdict="positive"
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "reaction_removed",
                "reaction": "+1",
                "user": "U1",
                "item": {"channel": "C1", "ts": "111.1"},
            },
        }
    ).encode()
    assert _post(client, body).status_code == 200
    assert conn.execute("SELECT COUNT(*) c FROM feedback_signal").fetchone()["c"] == 0


def test_reaction_on_unmapped_message_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SLACK_SIGNING_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    _seed_run(conn, channel="C1", ts="111.1")
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "reaction_added",
                "reaction": "+1",
                "user": "U1",
                "item": {"channel": "OTHER", "ts": "999.9"},
            },
        }
    ).encode()
    assert _post(client, body).json()["status"] == "ignored"
    assert conn.execute("SELECT COUNT(*) c FROM feedback_signal").fetchone()["c"] == 0


def test_unknown_emoji_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SLACK_SIGNING_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    _seed_run(conn)
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "reaction_added",
                "reaction": "eyes",
                "user": "U1",
                "item": {"channel": "C1", "ts": "111.1"},
            },
        }
    ).encode()
    assert _post(client, body).json()["status"] == "ignored"
    assert conn.execute("SELECT COUNT(*) c FROM feedback_signal").fetchone()["c"] == 0
