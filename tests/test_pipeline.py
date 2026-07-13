import asyncio
from contextlib import contextmanager

import pytest

from server.models import Finding
from server.pipeline import review_pr, PipelineDeps, PipelineError, _build_prompt
from server.repos import repo_repo, pr_repo, finding_repo, review_repo
from server.review.prescreen import MAX_INLINE_DIFF_CHARS


@pytest.fixture(autouse=True)
def _no_runtime_credentials(monkeypatch):
    monkeypatch.setattr(
        "server.review.harness.HarnessProfile.prepare_runtime",
        lambda self, runtime_dir: None,
    )


@contextmanager
def fake_worktree(repo, sha, pr_number=None):
    yield "/tmp/fake-wt"


class FakeAdapter:
    def __init__(self, vendor, findings):
        self.vendor = vendor
        self._f = findings

    async def review(self, **kw):  # ★개정: async
        return self._f


def test_pipeline_persists_findings_both_vendors(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="sha1",
        base_ref="main",
        url="u",
    )
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[
            FakeAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
            ),
            FakeAdapter(
                "codex", [Finding("codex", "b.py", 2, "low", "style", "c2", "r2", 0.4)]
            ),
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    fs = finding_repo.list_for_run(db, run_id)
    vendors = {f["vendor"] for f in fs}
    assert vendors == {"claude", "codex"}
    assert pr_repo.get(db, pid)["last_reviewed_sha"] == "sha1"


def test_pipeline_skips_on_trivial_prescreen(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=8,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "typo fix",
        worktree=fake_worktree,
        adapters=[],
        prescreen=lambda diff, model: ("trivial", 0.1, "오타"),
        repo_local_path="/tmp/x",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="auto", deps=deps))
    # skip → run 상태 canceled, finding 없음
    assert review_repo.get_run(db, run_id)["status"] == "canceled"
    assert finding_repo.list_for_run(db, run_id) == []


def test_pipeline_cancels_too_large_diff_before_vendor_review(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=11,
        title="big",
        author="a",
        head_sha="bigsha",
        base_ref="main",
        url="u",
    )

    @contextmanager
    def blocked_worktree(repo, sha, pr_number=None):
        raise AssertionError("worktree should not be prepared")
        yield

    class BlockedAdapter:
        vendor = "claude"

        async def review(self, **kw):
            raise AssertionError("adapter should not be called")

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "x" * (MAX_INLINE_DIFF_CHARS + 1),
        worktree=blocked_worktree,
        adapters=[BlockedAdapter()],
        prescreen=lambda diff, model: ("complex", 1.0, "diff too large"),
        repo_local_path="/tmp/x",
    )

    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    run = review_repo.get_run(db, run_id)
    assert run["status"] == "canceled"
    assert "diff too large" in run["error"]
    assert review_repo.list_vendor_results(db, run_id) == []


def test_pipeline_fails_run_when_all_vendors_fail(db):
    """★개정 (codex v4 [HIGH]): 벤더 전원 실패면 run=failed + PipelineError.
    (rate-limit/auth 실패를 done으로 오판하지 않고 worker가 retry하도록)"""
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=9,
        title="t",
        author="a",
        head_sha="s9",
        base_ref="main",
        url="u",
    )

    class FailAdapter:
        def __init__(self, vendor):
            self.vendor = vendor

        async def review(self, **kw):
            raise RuntimeError("rate limit exceeded")

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[FailAdapter("claude"), FailAdapter("codex")],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/x",
    )
    with pytest.raises(PipelineError) as ei:
        asyncio.run(review_pr(db, pr_id=pid, trigger="auto", deps=deps))
    assert review_repo.get_run(db, ei.value.run_id)["status"] == "failed"
    assert "rate limit" in str(ei.value)  # retry 판정 근거 문자열 보존


def test_pipeline_partial_success_one_vendor_fails(db):
    """부분 성공: 한 벤더 실패해도 다른 벤더 성공분은 run=done + 영속화."""
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=10,
        title="t",
        author="a",
        head_sha="s10",
        base_ref="main",
        url="u",
    )

    class FailAdapter:
        def __init__(self, vendor):
            self.vendor = vendor

        async def review(self, **kw):
            raise RuntimeError("boom")

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[
            FailAdapter("claude"),
            FakeAdapter(
                "codex", [Finding("codex", "b.py", 2, "low", "style", "c2", "r2", 0.4)]
            ),
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/x",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert review_repo.get_run(db, run_id)["status"] == "done"
    fs = finding_repo.list_for_run(db, run_id)
    assert {f["vendor"] for f in fs} == {"codex"}  # 성공 벤더 finding만 영속화
    vr = {v["vendor"]: v["status"] for v in review_repo.list_vendor_results(db, run_id)}
    assert vr == {"claude": "failed", "codex": "done"}


def test_pipeline_degrades_when_context_gather_raises(db):
    # 외부 컨텍스트 수집이 터져도 리뷰는 계속(run=done). B-INV-4/8 degrade.
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=12,
        title="t",
        author="a",
        head_sha="s12",
        base_ref="main",
        url="u",
    )

    class BoomContext:
        def gather(self, *, req):
            raise RuntimeError("provider exploded")

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[
            FakeAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
            )
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/x",
        context=BoomContext(),
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert review_repo.get_run(db, run_id)["status"] == "done"
    import json

    run = review_repo.get_run(db, run_id)
    assert run["context_text"] == ""
    meta = json.loads(run["context_meta"])
    assert meta.get("degraded") is True and meta["sources"] == []


def test_pipeline_redacts_error_in_persisted_meta(db, monkeypatch):
    import json
    from server import config
    from server.context.base import ContextResult

    monkeypatch.setattr(config, "JIRA_API_TOKEN", "tok-SEKRET")
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=22,
        title="t",
        author="a",
        head_sha="s22",
        base_ref="main",
        url="u",
    )

    class DirectErrCtx:
        def __init__(self):
            self.results = [
                ContextResult(
                    provider="jira",
                    status="error",
                    error="failed with tok-SEKRET in body",
                )
            ]

        def gather(self, *, req):
            return ""

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[
            FakeAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
            )
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/x",
        context=DirectErrCtx(),
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    meta = json.loads(review_repo.get_run(db, run_id)["context_meta"])
    assert "tok-SEKRET" not in (meta["sources"][0]["error"] or "")
    assert meta["sources"][0]["error"] == "failed with [redacted] in body"


def test_pipeline_persists_gathered_context(db):
    import json
    from server.context.base import ContextResult

    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=21,
        title="t",
        author="a",
        head_sha="s21",
        base_ref="main",
        url="u",
    )

    class FakeCtx:
        def __init__(self):
            self.results = [
                ContextResult(provider="static", status="ok", text="hello ctx")
            ]

        def gather(self, *, req):
            return "hello ctx"

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[
            FakeAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
            )
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/x",
        context=FakeCtx(),
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    run = review_repo.get_run(db, run_id)
    assert run["context_text"] == "hello ctx"
    meta = json.loads(run["context_meta"])
    assert (
        meta["sources"][0]["provider"] == "static"
        and meta["sources"][0]["status"] == "ok"
    )


def test_pipeline_passes_head_ref_and_body_to_context_request(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=22,
        title="t",
        author="a",
        head_sha="s22",
        base_ref="main",
        url="u",
        head_ref="feature/PROJ-1",
        body="Closes PROJ-1",
    )

    class SpyCtx:
        def __init__(self):
            self.results = []
            self.captured_req = None

        def gather(self, *, req):
            self.captured_req = req
            return ""

    spy = SpyCtx()
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[FakeAdapter("claude", [])],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/x",
        context=spy,
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert spy.captured_req.head_ref == "feature/PROJ-1"
    assert spy.captured_req.body == "Closes PROJ-1"


def test_pipeline_normalizes_null_head_ref_and_body(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=23,
        title="t",
        author="a",
        head_sha="s23",
        base_ref="main",
        url="u",
    )
    # 마이그레이션 이전 행 재현: nullable 컬럼이 NULL로 남아 있음
    db.execute("UPDATE pull_request SET head_ref=NULL, body=NULL WHERE id=?", (pid,))
    db.commit()

    class SpyCtx:
        def __init__(self):
            self.results = []
            self.captured_req = None

        def gather(self, *, req):
            self.captured_req = req
            return ""

    spy = SpyCtx()
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[FakeAdapter("claude", [])],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/x",
        context=spy,
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert spy.captured_req.head_ref == ""
    assert spy.captured_req.body == ""


def test_pipeline_injects_static_context_end_to_end(db, tmp_path):
    import json
    from server.repos import settings_repo
    from server.context.registry import build_context_provider

    (tmp_path / "ctx.md").write_text("아키텍처 결정: 큐 기반")

    rid = repo_repo.add(db, full_name="acme/api", local_path=str(tmp_path))
    repo_repo.update(
        db, rid, context_static_on=1, static_context_path=str(tmp_path / "ctx.md")
    )
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=40,
        title="t",
        author="a",
        head_sha="s40",
        base_ref="main",
        url="u",
    )
    repo = repo_repo.get(db, rid)
    settings = settings_repo.get(db)

    class CapturingAdapter:
        vendor = "claude"

        def __init__(self):
            self.prompt = None

        async def review(self, *, prompt, **kw):
            self.prompt = prompt
            return []

    cap = CapturingAdapter()
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[cap],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path=str(tmp_path),
        context=build_context_provider(repo, settings),
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))

    assert "## 외부 컨텍스트" in cap.prompt and "아키텍처 결정" in cap.prompt
    run = review_repo.get_run(db, run_id)
    assert "아키텍처 결정" in run["context_text"]
    meta = json.loads(run["context_meta"])
    assert (
        meta["sources"][0]["provider"] == "static"
        and meta["sources"][0]["status"] == "ok"
    )


def test_build_prompt_empty_no_block():
    out = _build_prompt({"number": 3, "title": "T", "author": "u"}, "DIFF", "")
    assert "## 외부 컨텍스트" not in out and "DIFF" in out


def test_build_prompt_with_block():
    out = _build_prompt({"number": 3, "title": "T", "author": "u"}, "DIFF", "### s\nhi")
    assert "## 외부 컨텍스트" in out and "hi" in out
