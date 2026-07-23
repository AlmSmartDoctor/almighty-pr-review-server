import json
import subprocess

import pytest

from server.github import gh


class FakeRunner:
    """subprocess 대체: 등록된 argv 프리픽스에 (stdout) 매핑."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        for prefix, out in self.mapping.items():
            if args[: len(prefix)] == list(prefix):
                return out
        raise AssertionError(f"unexpected call: {args}")


def test_list_open_prs_parses_json():
    payload = json.dumps(
        [
            {
                "number": 7,
                "title": "fix",
                "author": {"login": "kim"},
                "headRefOid": "abc",
                "baseRefName": "main",
                "baseRefOid": "base123",
                "url": "https://x/7",
                "state": "OPEN",
                "createdAt": "2026-07-07T11:22:33Z",
                "headRefName": "feature/PROJ-1",
                "body": "Closes PROJ-1",
                "isDraft": True,
            }
        ]
    )
    runner = FakeRunner(
        {
            ("gh", "pr", "list"): payload,
        }
    )
    client = gh.GhClient(runner=runner)
    prs = client.list_open_prs("acme/api")
    assert prs[0].number == 7
    assert prs[0].head_sha == "abc"
    assert prs[0].base_sha == "base123"
    assert prs[0].author == "kim"
    assert prs[0].created_at == "2026-07-07T11:22:33Z"
    assert prs[0].head_ref == "feature/PROJ-1"
    assert prs[0].body == "Closes PROJ-1"
    assert prs[0].is_draft is True
    assert any("baseRefOid" in arg for arg in runner.calls[0][0])
    assert any("createdAt" in arg for arg in runner.calls[0][0])
    assert any("headRefName" in arg for arg in runner.calls[0][0])
    assert any("isDraft" in arg for arg in runner.calls[0][0])
    assert any("body" in arg for arg in runner.calls[0][0])
    assert "--limit" in runner.calls[0][0]  # 재조정용 완전 오픈 셋 확보


def test_gets_current_pr_review_context_with_one_graphql_call():
    payload = {
        "data": {"repository": {"pullRequest": {
            "reviews": {"nodes": [{"id": "r1", "body": "summary"}]},
            "reviewThreads": {"nodes": [{
                "is_resolved": False,
                "comments": {"nodes": [{"id": "c1", "body": "inline"}]},
            }]},
            "comments": {"nodes": [{"id": "i1", "body": "conversation"}]},
        }}}
    }
    calls = []

    def runner(args, **kw):
        calls.append((args, kw))
        return json.dumps(payload)

    result = gh.GhClient(runner=runner).get_pr_review_context("acme/api", 7)

    assert len(calls) == 1 and calls[0][0][2] == "graphql"
    assert calls[0][1]["timeout"] == 5
    assert result["inline_comments"][0]["is_resolved"] is False
    assert result["conversation_comments"][0]["body"] == "conversation"


def test_current_pr_review_context_rejects_graphql_errors_for_rest_fallback():
    client = gh.GhClient(
        runner=lambda args, **kwargs: json.dumps(
            {"data": {"repository": None}, "errors": [{"message": "rate limited"}]}
        )
    )

    with pytest.raises(RuntimeError, match="GraphQL review context query failed"):
        client.get_pr_review_context("acme/api", 7)


def test_lists_current_pr_review_context_from_bounded_read_endpoints():
    payloads = {
        "/issues/7/comments?per_page=100": [{"id": 3, "body": "conversation"}],
        "/reviews?per_page=100": [{"id": 1, "body": "summary"}],
        "/comments?per_page=100": [{"id": 2, "body": "inline"}],
    }

    def runner(args, **kw):
        endpoint = args[2]
        for suffix, payload in payloads.items():
            if endpoint.endswith(suffix):
                return json.dumps(payload)
        raise AssertionError(endpoint)

    client = gh.GhClient(runner=runner)

    assert client.list_pr_reviews("acme/api", 7)[0]["body"] == "summary"
    assert client.list_pr_review_comments("acme/api", 7)[0]["body"] == "inline"
    assert client.list_pr_conversation_comments("acme/api", 7)[0]["body"] == "conversation"


def test_diff_returns_text():
    runner = FakeRunner({("gh", "pr", "diff"): "diff --git a b\n+x"})
    client = gh.GhClient(runner=runner)
    assert "diff --git" in client.diff("acme/api", 7)


def test_compare_diff_uses_three_dot_range_and_diff_media_type():
    runner = FakeRunner({("gh", "api"): "diff --git a b\n+delta"})
    client = gh.GhClient(runner=runner)
    out = client.compare_diff("acme/api", "base1", "head2")
    assert "delta" in out
    argv = runner.calls[0][0]
    assert "/repos/acme/api/compare/base1...head2" in argv
    assert "Accept: application/vnd.github.diff" in argv


def test_post_comment_returns_id_and_url():
    runner = FakeRunner(
        {
            (
                "gh",
                "api",
                "-X",
                "POST",
            ): '{"id": 99, "html_url": "https://x/7#issuecomment-99"}',
        }
    )
    client = gh.GhClient(runner=runner)
    res = client.post_comment("acme/api", 7, "hello")
    assert res["id"] == 99
    assert res["html_url"].endswith("issuecomment-99")
    # 유일한 write 경로 — issues/comments POST 엔드포인트인지 검증
    assert any(a[0][:2] == ["gh", "api"] and "POST" in a[0] for a in runner.calls)


def test_edit_comment_patches_in_place():
    runner = FakeRunner(
        {
            (
                "gh",
                "api",
                "-X",
                "PATCH",
            ): '{"id": 99, "html_url": "https://x/7#issuecomment-99"}'
        }
    )
    client = gh.GhClient(runner=runner)
    res = client.edit_comment("acme/api", "99", "updated")
    assert res["id"] == 99
    assert res["html_url"].endswith("issuecomment-99")
    assert any(a[0][:2] == ["gh", "api"] for a in runner.calls)


def test_create_review_submits_comment_event_with_inline_via_stdin():
    runner = FakeRunner(
        {
            (
                "gh",
                "api",
                "-X",
                "POST",
            ): '{"id": 555, "html_url": "https://x/pull/7#pullrequestreview-555"}',
        }
    )
    client = gh.GhClient(runner=runner)
    res = client.create_review(
        "acme/api",
        7,
        "headsha",
        "review body",
        [{"path": "a.py", "line": 3, "body": "inline"}],
    )
    assert res["id"] == 555
    argv = runner.calls[0][0]
    assert "/repos/acme/api/pulls/7/reviews" in argv
    assert "--input" in argv and "-" in argv  # 중첩 페이로드는 stdin으로
    payload = json.loads(runner.calls[0][1]["input"])
    assert payload["event"] == "COMMENT"
    assert payload["commit_id"] == "headsha"
    assert payload["body"] == "review body"
    assert payload["comments"] == [
        {"path": "a.py", "line": 3, "side": "RIGHT", "body": "inline"}
    ]


def test_create_review_without_comments_omits_comments_key():
    runner = FakeRunner({("gh", "api", "-X", "POST"): '{"id": 5, "html_url": "u"}'})
    client = gh.GhClient(runner=runner)
    client.create_review("acme/api", 7, "sha", "body only", [])
    payload = json.loads(runner.calls[0][1]["input"])
    assert "comments" not in payload  # 인라인 없으면 body-only review


def test_update_review_puts_body_only():
    runner = FakeRunner(
        {
            (
                "gh",
                "api",
                "-X",
                "PUT",
            ): '{"id": 9, "html_url": "https://x/pull/7#pullrequestreview-9"}',
        }
    )
    client = gh.GhClient(runner=runner)
    res = client.update_review("acme/api", 7, "9", "new body")
    assert res["id"] == 9
    argv = runner.calls[0][0]
    assert "/repos/acme/api/pulls/7/reviews/9" in argv
    assert "PUT" in argv


def test_env_prefers_gh_token_then_github_token():
    # GH_TOKEN·GITHUB_TOKEN은 gh가 네이티브로 읽는 표준 변수 → 항상 GH_TOKEN으로 노출.
    for env, expected in [
        (
            {
                "GH_TOKEN": "gh",
                "GITHUB_TOKEN": "gt",
                "GITHUB_PERSONAL_ACCESS_TOKEN": "pat",
            },
            "gh",
        ),
        ({"GITHUB_TOKEN": "gt", "GITHUB_PERSONAL_ACCESS_TOKEN": "pat"}, "gt"),
    ]:
        runner = FakeRunner({("gh", "api", "user"): '{"login": "me"}'})
        client = gh.GhClient(runner=runner, env=env)
        client.preflight_user()
        assert runner.calls[0][1]["env"]["GH_TOKEN"] == expected


def test_pat_promoted_only_without_native_gh_auth(monkeypatch):
    # 비표준 GITHUB_PERSONAL_ACCESS_TOKEN은 gh 자체 인증(keyring)이 없을 때만 GH_TOKEN으로
    # 승격한다. keyring 로그인이 있으면 약한 PAT가 그것을 덮어써 조직 private 레포를 404로
    # 깨뜨리므로 승격하지 않는다(프리뷰·포스팅 회귀 방지).
    for has_native, promoted in [(False, True), (True, False)]:
        monkeypatch.setattr(gh, "_gh_has_native_auth", lambda: has_native)
        runner = FakeRunner({("gh", "api", "user"): '{"login": "me"}'})
        client = gh.GhClient(runner=runner, env={"GITHUB_PERSONAL_ACCESS_TOKEN": "pat"})
        client.preflight_user()
        env = runner.calls[0][1]["env"]
        assert (env.get("GH_TOKEN") == "pat") is promoted


def test_timeout_becomes_retryable_gh_error():
    # gh 행이 폴러/워커를 영구 정지시키지 않게 timeout → GitHubCliError 변환.
    # 메시지의 "timed out"은 worker._RETRYABLE과 매칭돼 일시 장애로 재시도된다.
    def runner(args, **kw):
        raise subprocess.TimeoutExpired(args, 300)

    client = gh.GhClient(runner=runner, env={})
    try:
        client.diff("acme/api", 7)
        raise AssertionError("expected GitHubCliError")
    except gh.GitHubCliError as e:
        assert "timed out" in e.message
        assert e.command_kind == "diff"
        assert e.http_status is None


def test_default_runner_passes_timeout(monkeypatch):
    captured = {}

    class _Res:
        stdout = "ok"

    def fake_run(args, **kw):
        captured.update(kw)
        return _Res()

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    gh._default_runner(["gh", "api"], env=None)
    assert captured["timeout"] > 0


def test_default_runner_forwards_env_and_input(monkeypatch):
    # create_review의 stdin(--input -) 페이로드와 정규화 env가 실제로 subprocess.run에
    # 전달되는지 고정(이전엔 **kw를 흘려버려 유실됐음).
    captured = {}

    class _Res:
        stdout = "ok"

    def fake_run(args, **kw):
        captured.update(kw)
        return _Res()

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    out = gh._default_runner(["gh", "api"], env={"GH_TOKEN": "x"}, input='{"a":1}')
    assert out == "ok"
    assert captured["input"] == '{"a":1}'
    assert captured["env"] == {"GH_TOKEN": "x"}


def test_called_process_error_is_redacted_and_structured():
    secret = "secret-token"

    def runner(args, **kw):
        raise subprocess.CalledProcessError(
            1,
            args,
            stderr=f"HTTP 403: denied {secret}",
        )

    client = gh.GhClient(
        runner=runner,
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": secret},
    )
    try:
        client.preflight_repo("acme/api")
        raise AssertionError("expected GitHubCliError")
    except gh.GitHubCliError as e:
        assert e.exit_code == 1
        assert e.http_status == 403
        assert e.command_kind == "preflight_repo"
        assert secret not in e.message
        assert "[redacted]" in e.message
