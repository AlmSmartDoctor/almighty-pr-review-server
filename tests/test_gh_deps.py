from server.pipeline import PipelineDeps
from server.context.composite import CompositeContextProvider
from server.review.gh_deps import build_deps


def test_build_deps_allows_missing_local_path():
    # local_path 없어도 deps를 조립한다 — 리뷰 시 clone으로 온디맨드 체크아웃.
    deps = build_deps(
        {"full_name": "acme/api", "local_path": None, "harness_name": "default"}, {}
    )
    assert deps.repo_local_path is None
    assert callable(deps.clone)  # 온디맨드 clone 배선됨


def test_build_deps_assembles_pipeline_deps():
    deps = build_deps(
        {"full_name": "acme/api", "local_path": "/tmp/acme", "harness_name": "default"},
        {},
    )
    assert isinstance(deps, PipelineDeps)
    assert deps.repo_local_path == "/tmp/acme"
    assert [a.vendor for a in deps.adapters] == ["claude", "codex"]
    assert callable(deps.gh_diff) and callable(deps.prescreen)
    assert callable(deps.clone)
    assert isinstance(deps.context, CompositeContextProvider)


def test_build_deps_filters_verify_adapters_by_vendor_toggle(monkeypatch):
    # OFF 벤더를 refuter로 exec하면 매번 실패해 검증이 조용히 무력화된다.
    captured = {}

    def fake_make_verifier(adapters, worktree, clone=None, **kwargs):
        captured["vendors"] = [a.vendor for a in adapters]
        return "verifier"

    monkeypatch.setattr("server.review.gh_deps.make_verifier", fake_make_verifier)
    deps = build_deps(
        {
            "full_name": "acme/api",
            "local_path": None,
            "harness_name": "default",
            "vendor_claude_on": 1,
            "vendor_codex_on": 0,
        },
        {},
    )
    assert captured["vendors"] == ["claude"]
    assert deps.verify == "verifier"
    assert [a.vendor for a in deps.adapters] == ["claude", "codex"]  # 리뷰 목록 불변


def test_build_deps_includes_static_provider_when_enabled():
    from server.context.static_provider import StaticContextProvider

    deps = build_deps(
        {
            "full_name": "acme/api",
            "local_path": "/tmp/acme",
            "harness_name": "default",
            "context_static_on": 1,
            "static_context_path": "/tmp/acme/ctx.md",
        },
        {"context_static_on": 0},
    )
    assert any(isinstance(p, StaticContextProvider) for p in deps.context.providers)


def test_build_deps_includes_static_provider_without_configured_path():
    from server.context.static_provider import StaticContextProvider

    deps = build_deps(
        {
            "full_name": "acme/api",
            "local_path": "/tmp/acme",
            "harness_name": "default",
            "context_static_on": 1,
        },
        {"context_static_on": 0},
    )
    assert any(isinstance(p, StaticContextProvider) for p in deps.context.providers)


def test_build_deps_includes_jira_provider_when_configured(monkeypatch):
    from server import config
    from server.context.jira_provider import JiraContextProvider

    monkeypatch.setattr(config, "JIRA_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setattr(config, "JIRA_EMAIL", "me@acme.com")
    monkeypatch.setattr(config, "JIRA_API_TOKEN", "tok")
    deps = build_deps(
        {
            "full_name": "acme/api",
            "local_path": "/tmp/acme",
            "harness_name": "default",
            "context_jira_on": 1,
        },
        {"context_jira_on": 0},
    )
    assert any(isinstance(p, JiraContextProvider) for p in deps.context.providers)


def test_build_deps_skips_jira_when_token_unset(monkeypatch):
    from server import config
    from server.context.jira_provider import JiraContextProvider

    monkeypatch.setattr(config, "JIRA_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setattr(config, "JIRA_EMAIL", "me@acme.com")
    monkeypatch.setattr(config, "JIRA_API_TOKEN", "")
    deps = build_deps(
        {
            "full_name": "acme/api",
            "local_path": "/tmp/acme",
            "harness_name": "default",
            "context_jira_on": 1,
        },
        {"context_jira_on": 0},
    )
    assert not any(isinstance(p, JiraContextProvider) for p in deps.context.providers)
