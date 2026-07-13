import json
import subprocess

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
                "url": "https://x/7",
                "state": "OPEN",
                "createdAt": "2026-07-07T11:22:33Z",
                "headRefName": "feature/PROJ-1",
                "body": "Closes PROJ-1",
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
    assert prs[0].author == "kim"
    assert prs[0].created_at == "2026-07-07T11:22:33Z"
    assert prs[0].head_ref == "feature/PROJ-1"
    assert prs[0].body == "Closes PROJ-1"
    assert any("createdAt" in arg for arg in runner.calls[0][0])
    assert any("headRefName" in arg for arg in runner.calls[0][0])
    assert any("body" in arg for arg in runner.calls[0][0])


def test_diff_returns_text():
    runner = FakeRunner({("gh", "pr", "diff"): "diff --git a b\n+x"})
    client = gh.GhClient(runner=runner)
    assert "diff --git" in client.diff("acme/api", 7)


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


def test_env_prefers_gh_token_then_github_token_then_pat():
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
        ({"GITHUB_PERSONAL_ACCESS_TOKEN": "pat"}, "pat"),
    ]:
        runner = FakeRunner({("gh", "api", "user"): '{"login": "me"}'})
        client = gh.GhClient(runner=runner, env=env)
        client.preflight_user()
        assert runner.calls[0][1]["env"]["GH_TOKEN"] == expected


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
