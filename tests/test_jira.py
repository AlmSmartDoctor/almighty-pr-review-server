import os

import pytest

from server import config
from server.context import jira_client


class FakeResponse:
    def __init__(self, status_code, data, *, content=None):
        self.status_code = status_code
        self._data = data
        self.content = content

    def json(self):
        return self._data


class FakeHttp:
    """httpx 대체: .get(url, **kw) 호출을 기록하고 등록된 응답/예외를 되돌려준다."""

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc is not None:
            raise self.exc
        return self.response


class FakeStreamResponse:
    def __init__(self, chunks, *, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_bytes(self):
        yield from self._chunks


class FakeStreamingHttp:
    def __init__(self, response):
        self.response = response

    def stream(self, method, url, **kwargs):
        return self.response


def _client(
    http,
    base_url="https://acme.atlassian.net",
    acceptance_criteria_field="",
):
    return jira_client.JiraClient(
        base_url=base_url,
        email="me@acme.com",
        token="tok-secret",
        http=http,
        acceptance_criteria_field=acceptance_criteria_field,
    )


def test_get_issue_happy_parse():
    http = FakeHttp(
        FakeResponse(
            200,
            {"fields": {"summary": "로그인 버그", "description": "재현 절차..."}},
        )
    )
    client = _client(http)
    result = client.get_issue("PROJ-1")
    assert result == {
        "key": "PROJ-1",
        "summary": "로그인 버그",
        "description": "재현 절차...",
        "acceptance_criteria": "",
    }
    url, kw = http.calls[0]
    assert url.endswith("/rest/api/3/issue/PROJ-1")
    assert kw["auth"] == ("me@acme.com", "tok-secret")


def test_get_issue_adf_description_flattens_to_text():
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}
        ],
    }
    http = FakeHttp(FakeResponse(200, {"fields": {"summary": "s", "description": adf}}))
    client = _client(http)
    result = client.get_issue("PROJ-1")
    assert "hello" in result["description"]


def test_get_issue_fetches_acceptance_criteria_custom_field():
    http = FakeHttp(
        FakeResponse(
            200,
            {
                "fields": {
                    "summary": "s",
                    "description": "d",
                    "customfield_12345": {
                        "type": "doc",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "must pass"}],
                            }
                        ],
                    },
                }
            },
        )
    )
    result = _client(http, acceptance_criteria_field="customfield_12345").get_issue(
        "PROJ-1"
    )

    assert result["acceptance_criteria"] == "must pass"
    assert "customfield_12345" in http.calls[0][1]["params"]["fields"]


def test_http_error_is_redacted_and_structured():
    http = FakeHttp(FakeResponse(403, {}))
    client = _client(http)
    try:
        client.get_issue("PROJ-1")
        raise AssertionError("expected JiraError")
    except jira_client.JiraError as e:
        assert e.http_status == 403
        assert "tok-secret" not in str(e)
        assert "me@acme.com" not in str(e)


def test_raising_http_is_redacted():
    http = FakeHttp(exc=RuntimeError("boom tok-secret"))
    client = _client(http)
    try:
        client.get_issue("PROJ-1")
        raise AssertionError("expected JiraError")
    except jira_client.JiraError as e:
        assert "tok-secret" not in str(e)
        assert "[redacted]" in str(e)


def test_ssrf_rejected_at_fetch_not_construction():
    for base_url in (
        "https://127.0.0.1",
        "http://acme.atlassian.net",
        "https://10.0.0.5",
        "https://169.254.169.254",
        "https://localhost",
        "https://foo.local",
        "https://foo.internal",
        "https://127.0.0.1.nip.io",
        "https://evil.example.com",
    ):
        http = FakeHttp(FakeResponse(200, {"fields": {}}))
        client = _client(http, base_url=base_url)  # construction must not raise
        try:
            client.get_issue("PROJ-1")
            raise AssertionError(f"expected JiraError for {base_url!r}")
        except jira_client.JiraError:
            pass
        assert http.calls == [], f"http.get must not be called for {base_url!r}"


def test_strict_key_rejected_without_calling_http():
    http = FakeHttp(FakeResponse(200, {"fields": {}}))
    client = _client(http)
    for bad_key in ("PROJ-1; rm -rf", "../admin"):
        try:
            client.get_issue(bad_key)
            raise AssertionError(f"expected JiraError for {bad_key!r}")
        except jira_client.JiraError:
            pass
    assert http.calls == []


class RaisingJsonResponse:
    def __init__(self, status_code, exc):
        self.status_code = status_code
        self._exc = exc

    def json(self):
        raise self._exc


def test_get_issue_non_json_200_raises_structured_redacted():
    http = FakeHttp(
        RaisingJsonResponse(200, ValueError("Expecting value: <html> tok-secret"))
    )
    client = _client(http)
    try:
        client.get_issue("PROJ-1")
        raise AssertionError("expected JiraError")
    except jira_client.JiraError as e:
        assert "tok-secret" not in str(e)
        assert "[redacted]" in str(e)


def test_get_issue_non_dict_json_raises():
    http = FakeHttp(FakeResponse(200, ["x"]))
    client = _client(http)
    try:
        client.get_issue("PROJ-1")
        raise AssertionError("expected JiraError")
    except jira_client.JiraError:
        pass


def test_summary_is_truncated_to_size_cap():
    long_summary = "x" * (config.MAX_CONTEXT_CHARS_PER_SOURCE + 500)
    http = FakeHttp(
        FakeResponse(200, {"fields": {"summary": long_summary, "description": ""}})
    )
    client = _client(http)
    result = client.get_issue("PROJ-1")
    assert len(result["summary"]) == config.MAX_CONTEXT_CHARS_PER_SOURCE


def test_response_body_is_rejected_before_json_parse_when_too_large(monkeypatch):
    monkeypatch.setattr(jira_client, "_MAX_RESPONSE_BYTES", 32)
    http = FakeHttp(FakeResponse(200, {}, content=b"x" * 33))

    with pytest.raises(jira_client.JiraError, match="response too large"):
        _client(http).get_issue("PROJ-1")


def test_streaming_response_stops_when_byte_cap_is_exceeded(monkeypatch):
    monkeypatch.setattr(jira_client, "_MAX_RESPONSE_BYTES", 8)
    http = FakeStreamingHttp(FakeStreamResponse([b"1234", b"56789"]))

    with pytest.raises(jira_client.JiraError, match="response too large"):
        _client(http).get_issue("PROJ-1")


@pytest.mark.skipif(
    not os.environ.get("ALMIGHTY_JIRA_E2E"),
    reason="set ALMIGHTY_JIRA_E2E=1 + ALMIGHTY_JIRA_* creds",
)
def test_jira_real_roundtrip():
    client = jira_client.JiraClient(
        base_url=config.JIRA_BASE_URL,
        email=config.JIRA_EMAIL,
        token=config.JIRA_API_TOKEN,
    )
    result = client.get_issue(os.environ["ALMIGHTY_JIRA_E2E_KEY"])
    assert result["key"]
