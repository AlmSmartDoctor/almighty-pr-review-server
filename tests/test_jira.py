from server import config
from server.context import jira_client


class FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

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


def _client(http, base_url="https://acme.atlassian.net"):
    return jira_client.JiraClient(
        base_url=base_url, email="me@acme.com", token="tok-secret", http=http
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


def test_summary_is_truncated_to_size_cap():
    long_summary = "x" * (config.MAX_CONTEXT_CHARS_PER_SOURCE + 500)
    http = FakeHttp(
        FakeResponse(200, {"fields": {"summary": long_summary, "description": ""}})
    )
    client = _client(http)
    result = client.get_issue("PROJ-1")
    assert len(result["summary"]) == config.MAX_CONTEXT_CHARS_PER_SOURCE
