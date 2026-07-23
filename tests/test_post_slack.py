import pytest
from fastapi.testclient import TestClient

from server import config
from server.api import app, get_conn, get_gh, get_slack
from server.db import connect, init_schema
from server.repos import feedback_repo, finding_repo, pr_repo, repo_repo, review_repo


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


class HealthyGh:
    def preflight_user(self):
        return {"login": "me"}

    def preflight_repo(self, repo):
        return {"full_name": repo}

    def preflight_issue(self, repo, number):
        return {"number": number}

    def diff(self, repo, number):
        return ""

    def get_pr_review_context(self, repo, number):
        return {"reviews": [], "inline_comments": [], "conversation_comments": []}

    def create_review(self, repo, number, commit_id, body, comments):
        return {"id": 1, "html_url": "https://x/pull/5#pullrequestreview-1"}

    def update_review(self, repo, number, review_id, body):
        return {"id": review_id, "html_url": "https://x/pull/5#pullrequestreview-1"}


class FakeSlack:
    def __init__(self, ts="900.1", channel="C1", exc=None):
        self.calls = []
        self._ts, self._channel, self._exc = ts, channel, exc

    def post_message(self, *, channel, text):
        self.calls.append({"channel": channel, "text": text})
        if self._exc:
            raise self._exc
        return {"channel": self._channel, "ts": self._ts}


def _setup(tmp_path):
    conn = connect(tmp_path / "p.db")
    init_schema(conn)
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=5,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="https://x/pull/5",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    return conn, run_id


def _approve(conn, run_id, claim="c"):
    fid = finding_repo.add(
        conn,
        run_id=run_id,
        vendor="claude",
        file="a.py",
        line=1,
        severity="high",
        category="bug",
        claim=claim,
        rationale="r",
        confidence=0.9,
    )
    finding_repo.set_status(conn, fid, "approved")
    return fid


def _mount(conn, gh, slack, monkeypatch, channel="#reviews"):
    monkeypatch.setattr(config, "SLACK_CHANNEL", channel)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_gh] = lambda: gh
    app.dependency_overrides[get_slack] = lambda: slack
    return TestClient(app)


def test_post_run_posts_to_slack_and_records_mapping(tmp_path, monkeypatch):
    conn, run_id = _setup(tmp_path)
    _approve(conn, run_id)
    slack = FakeSlack(ts="900.1", channel="C1")
    client = _mount(conn, HealthyGh(), slack, monkeypatch)
    r = client.post(f"/api/runs/{run_id}/post")
    assert r.status_code == 200 and r.json()["posted"]
    assert len(slack.calls) == 1
    assert slack.calls[0]["channel"] == "#reviews"
    assert feedback_repo.run_for_message(conn, channel="C1", ts="900.1") == run_id


def test_slack_post_is_idempotent_across_reposts(tmp_path, monkeypatch):
    conn, run_id = _setup(tmp_path)
    _approve(conn, run_id, claim="first")
    slack = FakeSlack()
    client = _mount(conn, HealthyGh(), slack, monkeypatch)
    client.post(f"/api/runs/{run_id}/post")
    # 새 finding 승인 후 재게시 — GitHub review는 갱신되지만 Slack은 이미 게시돼 재게시 안 함.
    _approve(conn, run_id, claim="second")
    client.post(f"/api/runs/{run_id}/post")
    assert len(slack.calls) == 1


def test_slack_failure_does_not_break_posting(tmp_path, monkeypatch):
    conn, run_id = _setup(tmp_path)
    _approve(conn, run_id)
    slack = FakeSlack(exc=RuntimeError("slack down"))
    client = _mount(conn, HealthyGh(), slack, monkeypatch)
    r = client.post(f"/api/runs/{run_id}/post")
    assert r.status_code == 200 and r.json()["posted"]
    assert feedback_repo.has_slack_post(conn, run_id) is False


def test_no_slack_when_disabled(tmp_path, monkeypatch):
    conn, run_id = _setup(tmp_path)
    _approve(conn, run_id)
    client = _mount(conn, HealthyGh(), None, monkeypatch, channel="")
    r = client.post(f"/api/runs/{run_id}/post")
    assert r.status_code == 200 and r.json()["posted"]
    assert feedback_repo.has_slack_post(conn, run_id) is False
