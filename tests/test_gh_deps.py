import pytest

from server.pipeline import PipelineDeps
from server.context.composite import CompositeContextProvider
from server.review.gh_deps import build_deps


def test_build_deps_requires_local_path():
    with pytest.raises(ValueError):
        build_deps(
            {"full_name": "acme/api", "local_path": None, "harness_name": "default"}, {}
        )


def test_build_deps_assembles_pipeline_deps():
    deps = build_deps(
        {"full_name": "acme/api", "local_path": "/tmp/acme", "harness_name": "default"},
        {},
    )
    assert isinstance(deps, PipelineDeps)
    assert deps.repo_local_path == "/tmp/acme"
    assert [a.vendor for a in deps.adapters] == ["claude", "codex"]
    assert callable(deps.gh_diff) and callable(deps.prescreen)
    assert isinstance(deps.context, CompositeContextProvider)


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
