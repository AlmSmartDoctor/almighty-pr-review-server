import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from server import config
from server.api import app, get_conn
from server.db import connect, init_schema
from server.github.webhook import parse_pull_request_event, verify_signature
from server.repos import repo_repo

_SECRET = "s3cr3t"


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _client(tmp_path):
    # test_api.py와 동일한 hermetic 패턴: with TestClient 미사용 → lifespan(poller/worker) 미기동
    conn = connect(tmp_path / "wh.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    return TestClient(app), conn


def _pr_body(full_name="acme/api", number=7, sha="sha1", action="opened"):
    return json.dumps(
        {
            "action": action,
            "repository": {"full_name": full_name},
            "pull_request": {
                "number": number,
                "title": "add feature",
                "user": {"login": "octocat"},
                "html_url": "https://github.com/acme/api/pull/7",
                "state": "open",
                "body": "closes #1",
                "head": {"sha": sha, "ref": "feature"},
                "base": {"ref": "main", "sha": "base-sha-1"},
            },
        }
    ).encode()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _post(client, body, *, sig=None, event="pull_request"):
    headers = {"X-GitHub-Event": event}
    if sig is not None:
        headers["X-Hub-Signature-256"] = sig
    return client.post("/api/webhooks/github", content=body, headers=headers)


def _job_count(conn):
    return conn.execute("SELECT COUNT(*) c FROM review_job").fetchone()["c"]


def test_verify_signature_roundtrip():
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert verify_signature("k", body, good) is True
    assert verify_signature("k", body, "sha256=bad") is False
    assert verify_signature("", body, good) is False  # 시크릿 미설정
    assert verify_signature("k", body, None) is False  # 헤더 없음


def test_parse_pull_request_event_filters_actions():
    info = parse_pull_request_event(_pr_body(action="synchronize"))
    assert info["head_sha"] == "sha1" and info["full_name"] == "acme/api"
    assert info["base_ref"] == "main" and info["base_sha"] == "base-sha-1"
    assert info["author"] == "octocat"
    assert parse_pull_request_event(_pr_body(action="labeled")) is None  # 무관 action
    assert parse_pull_request_event(b"not json") is None  # 파싱 실패


def test_verify_signature_nonascii_header_returns_false():
    # Starlette가 헤더 raw 바이트를 latin-1로 디코드 → 비ASCII str. raise 없이 False여야 함
    assert verify_signature("k", b"body", "sha256=\xff") is False
    assert verify_signature("k", b"body", "sha256=☃") is False


def test_parse_pull_request_event_non_dict_nested_fields_ignored():
    # 유효 서명이어도 비-dict 중첩(list/str/number)이 오면 raise 없이 None(무시)
    for pr in ("x", 123, [1, 2]):
        b = json.dumps(
            {"action": "opened", "pull_request": pr, "repository": {"full_name": "a/b"}}
        ).encode()
        assert parse_pull_request_event(b) is None
    b = json.dumps(
        {
            "action": "opened",
            "pull_request": {"head": "abc", "number": 1},
            "repository": {"full_name": "a/b"},
        }
    ).encode()
    assert parse_pull_request_event(b) is None  # head가 str → sha 못 뽑음 → None


def test_webhook_enqueues_on_valid_pull_request(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    repo_repo.add(conn, full_name="acme/api")  # 기본: enabled=1, auto, 벤더 on
    body = _pr_body(sha="sha1")
    r = _post(client, body, sig=_sign(body))
    assert r.status_code == 200 and r.json()["status"] == "enqueued"
    jobs = conn.execute("SELECT * FROM review_job").fetchall()
    assert len(jobs) == 1
    assert jobs[0]["head_sha"] == "sha1" and jobs[0]["trigger"] == "auto"
    assert conn.execute("SELECT base_sha FROM pull_request").fetchone()["base_sha"] == "base-sha-1"


def test_webhook_rejects_bad_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    repo_repo.add(conn, full_name="acme/api")
    body = _pr_body()
    r = _post(client, body, sig="sha256=deadbeef")
    assert r.status_code == 401 and _job_count(conn) == 0


def test_webhook_503_when_secret_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", "")
    client, conn = _client(tmp_path)
    body = _pr_body()
    r = _post(client, body, sig=_sign(body))
    assert r.status_code == 503 and _job_count(conn) == 0


def test_webhook_ignores_non_pr_event(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    repo_repo.add(conn, full_name="acme/api")
    body = json.dumps({"zen": "hi"}).encode()
    r = _post(client, body, sig=_sign(body), event="ping")
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert _job_count(conn) == 0


def test_webhook_ignores_unrelated_action(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    repo_repo.add(conn, full_name="acme/api")
    body = _pr_body(action="labeled")
    r = _post(client, body, sig=_sign(body))
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert _job_count(conn) == 0


def test_webhook_skips_manual_trigger_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    rid = repo_repo.add(conn, full_name="acme/api")
    repo_repo.update(conn, rid, trigger_mode="manual")  # poller와 동일하게 자동 스킵
    body = _pr_body()
    r = _post(client, body, sig=_sign(body))
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert _job_count(conn) == 0


def test_webhook_skips_when_no_vendor(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    rid = repo_repo.add(conn, full_name="acme/api")
    repo_repo.update(conn, rid, vendor_claude_on=0, vendor_codex_on=0)
    body = _pr_body()
    r = _post(client, body, sig=_sign(body))
    # 벤더 없음 → PR upsert는 되지만 enqueue 스킵(poller 동일)
    assert r.status_code == 200 and r.json()["status"] == "skipped"
    assert _job_count(conn) == 0


def test_webhook_ignores_unregistered_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", _SECRET)
    client, conn = _client(tmp_path)  # 레포 미등록
    body = _pr_body(full_name="ghost/repo")
    r = _post(client, body, sig=_sign(body))
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert _job_count(conn) == 0


def test_webhook_nonascii_signature_is_401_not_500(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    repo_repo.add(conn, full_name="acme/api")
    body = _pr_body()
    # 실제 GitHub처럼 raw 바이트 헤더(0xFF) → Starlette가 latin-1 디코드 → 비ASCII str.
    # compare_digest(str,str) TypeError를 회피해 500 아닌 401이어야 함
    r = client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": b"sha256=\xff",
        },
    )
    assert r.status_code == 401 and _job_count(conn) == 0


def test_webhook_malformed_payload_is_ignored_not_500(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    repo_repo.add(conn, full_name="acme/api")
    # 유효 서명 + 비-dict 중첩 payload → 500 아니라 2xx 무시
    body = json.dumps(
        {"action": "opened", "pull_request": "not-an-object", "repository": {}}
    ).encode()
    r = _post(client, body, sig=_sign(body))
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert _job_count(conn) == 0


def test_webhook_repo_lookup_is_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    repo_repo.add(conn, full_name="Acme/API")  # 등록 casing
    body = _pr_body(full_name="acme/api")  # GitHub 정규 casing
    r = _post(client, body, sig=_sign(body))
    assert r.status_code == 200 and r.json()["status"] == "enqueued"
    assert _job_count(conn) == 1


def test_webhook_skips_draft_pr(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    repo_repo.add(conn, full_name="acme/api")
    payload = json.loads(_pr_body())
    payload["pull_request"]["draft"] = True
    body = json.dumps(payload).encode()
    r = _post(client, body, sig=_sign(body))
    assert r.status_code == 200 and r.json()["status"] == "skipped"
    assert _job_count(conn) == 0


def test_webhook_ready_for_review_action_enqueues(tmp_path, monkeypatch):
    # draft → ready 전환 시 웹훅으로도 즉시 리뷰가 시작돼야 한다(폴링 대기 불필요).
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", _SECRET)
    client, conn = _client(tmp_path)
    repo_repo.add(conn, full_name="acme/api")
    body = _pr_body(action="ready_for_review")
    r = _post(client, body, sig=_sign(body))
    assert r.status_code == 200 and r.json()["status"] == "enqueued"
    assert _job_count(conn) == 1
