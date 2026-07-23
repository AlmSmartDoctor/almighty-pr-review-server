import asyncio

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from server import config
from server.api import _bounded_webhook_body, app


def test_management_api_requires_bearer_when_configured(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "a" * 32)
    client = TestClient(app)

    missing = client.get("/api/settings")
    bad = client.get("/api/settings", headers={"Authorization": "Bearer bad"})
    good = client.get(
        "/api/settings", headers={"Authorization": f"Bearer {'a' * 32}"}
    )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert bad.status_code == 401
    assert good.status_code == 200


def test_management_api_rejects_untrusted_origin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    client = TestClient(app)

    response = client.post(
        "/api/repos/sync", headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403


def test_public_webhooks_allow_trailing_slash_without_admin_token(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "a" * 32)
    client = TestClient(app)

    github = client.post("/api/webhooks/github/", content=b"{}", follow_redirects=False)
    slack = client.post("/api/webhooks/slack/", content=b"{}", follow_redirects=False)

    assert github.status_code != 401
    assert slack.status_code != 401


def test_webhook_rejects_content_length_before_reading_body(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(config, "WEBHOOK_MAX_BODY_BYTES", 4)
    response = TestClient(app).post(
        "/api/webhooks/github",
        content=b"12345",
        headers={"X-Hub-Signature-256": "sha256=bad"},
    )
    assert response.status_code == 413


def test_chunked_webhook_body_stops_at_stream_cap(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_MAX_BODY_BYTES", 4)
    messages = iter((
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"45", "more_body": False},
    ))

    async def receive():
        return next(messages)

    request = Request({
        "type": "http", "method": "POST", "path": "/", "headers": []
    }, receive)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(_bounded_webhook_body(request))
    assert caught.value.status_code == 413


def test_allowed_origin_receives_cors_headers_for_preflight(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    origin = "https://review.example.com"
    monkeypatch.setattr(config, "ADMIN_ALLOWED_ORIGINS", (origin,))
    client = TestClient(app)

    response = client.options("/api/settings", headers={"Origin": origin})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "Authorization" in response.headers["access-control-allow-headers"]
    assert "PATCH" in response.headers["access-control-allow-methods"]


def test_health_and_webhooks_remain_public(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "a" * 32)
    client = TestClient(app)

    health = client.get("/api/health")
    webhook = client.post("/api/webhooks/github", content=b"{}")

    assert health.status_code == 200
    assert health.json()["admin_auth_required"] is True
    assert webhook.status_code == 503  # provider HMAC 설정만 검사; admin bearer는 불필요
