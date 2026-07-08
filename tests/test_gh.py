import json

from server.github import gh


class FakeRunner:
    """subprocess 대체: 등록된 argv 프리픽스에 (stdout) 매핑."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append(args)
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
    assert any(a[:2] == ["gh", "api"] and "POST" in a for a in runner.calls)


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
    assert any(a[:2] == ["gh", "api"] for a in runner.calls)
