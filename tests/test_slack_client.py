import pytest

from server.slack.client import SlackClient, SlackError


class FakeResp:
    def __init__(self, status_code=200, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = (
            payload
            if payload is not None
            else {"ok": True, "channel": "C1", "ts": "111.1"}
        )
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("bad json")
        return self._payload


class FakeHttp:
    def __init__(self, resp=None, exc=None):
        self.resp = resp or FakeResp()
        self.exc = exc
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        if self.exc:
            raise self.exc
        return self.resp


def test_post_message_success_returns_channel_ts():
    http = FakeHttp(FakeResp(payload={"ok": True, "channel": "C9", "ts": "999.1"}))
    res = SlackClient(token="xoxb-secret", http=http).post_message(
        channel="C9", text="hi"
    )
    assert res == {"channel": "C9", "ts": "999.1"}
    call = http.calls[0]
    assert call["url"].endswith("/chat.postMessage")
    assert call["headers"]["Authorization"] == "Bearer xoxb-secret"
    assert call["json"] == {"channel": "C9", "text": "hi"}
    assert call["timeout"] == 5


def test_ok_false_raises_slackerror_without_leaking_token():
    http = FakeHttp(FakeResp(payload={"ok": False, "error": "channel_not_found"}))
    with pytest.raises(SlackError) as ei:
        SlackClient(token="xoxb-secret", http=http).post_message(channel="C", text="x")
    assert "channel_not_found" in str(ei.value)
    assert "xoxb-secret" not in str(ei.value)


def test_http_error_status_carries_status():
    http = FakeHttp(FakeResp(status_code=500, payload={}))
    with pytest.raises(SlackError) as ei:
        SlackClient(token="t", http=http).post_message(channel="C", text="x")
    assert ei.value.http_status == 500


def test_network_failure_redacts_token():
    http = FakeHttp(exc=RuntimeError("connect to xoxb-secret failed"))
    with pytest.raises(SlackError) as ei:
        SlackClient(token="xoxb-secret", http=http).post_message(channel="C", text="x")
    assert "xoxb-secret" not in str(ei.value)
    assert "[redacted]" in str(ei.value)


def test_malformed_json_raises():
    with pytest.raises(SlackError):
        SlackClient(token="t", http=FakeHttp(FakeResp(raise_json=True))).post_message(
            channel="C", text="x"
        )
