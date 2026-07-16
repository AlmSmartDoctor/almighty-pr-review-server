import asyncio
from contextlib import contextmanager

import pytest

from server.models import Finding
from server.pipeline import (
    review_pr,
    retry_pr,
    PipelineDeps,
    PipelineError,
    _build_prompt,
)
from server.repos import repo_repo, pr_repo, finding_repo, review_repo
from server.review.prescreen import PRESCREEN_FALLBACK_REASON


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


def test_pipeline_reviews_without_local_path_via_ondemand_clone(db):
    """local_path 미설정 레포도 gh 온디맨드 clone으로 auto 리뷰가 완주한다(자동 리뷰 언블록)."""
    rid = repo_repo.add(db, full_name="acme/api")  # local_path 없음
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=80,
        title="t",
        author="a",
        head_sha="s80",
        base_ref="main",
        url="u",
    )
    cloned = {}

    def clone(full_name, dest):
        cloned["full_name"] = full_name

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        clone=clone,
        adapters=[
            FakeAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
            )
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path=None,
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="auto", deps=deps))
    assert cloned["full_name"] == "acme/api"  # 온디맨드 clone 호출됨
    assert review_repo.get_run(db, run_id)["status"] == "done"
    assert {f["vendor"] for f in finding_repo.list_for_run(db, run_id)} == {"claude"}


def test_pipeline_records_step_durations(db):
    """prescreen·벤더 단계별 소요시간이 실측 저장된다(하드코딩 0/미기록 아님)."""
    import time as _time

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

    def slow_prescreen(diff, model):
        _time.sleep(0.02)
        return ("complex", 0.9, "핵심 로직")

    class SlowAdapter:
        def __init__(self, vendor):
            self.vendor = vendor

        async def review(self, **kw):
            await asyncio.sleep(0.02)
            return [Finding(self.vendor, "a.py", 1, "high", "bug", "c", "r", 0.8)]

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[SlowAdapter("claude"), SlowAdapter("codex")],
        prescreen=slow_prescreen,
        repo_local_path="/tmp/acme-api",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))

    ps = db.execute(
        "SELECT duration_ms FROM pre_screen WHERE pr_id=? ORDER BY id DESC LIMIT 1",
        (pid,),
    ).fetchone()
    assert ps["duration_ms"] is not None and ps["duration_ms"] > 0

    vrs = review_repo.list_vendor_results(db, run_id)
    assert all(v["status"] == "done" for v in vrs)
    assert all(v["duration_ms"] is not None and v["duration_ms"] > 0 for v in vrs)

    # 외부 컨텍스트 단계 소요시간도 감사에 기록(트레이스 표시용)
    import json

    meta = json.loads(review_repo.get_run(db, run_id)["context_meta"])
    assert meta["duration_ms"] is not None and meta["duration_ms"] >= 0


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


def test_manual_trigger_reviews_even_below_prescreen_threshold(db):
    """사람이 '리뷰' 버튼으로 트리거하면(trigger='manual') prescreen이 skip으로 판정해도
    취소하지 않고 항상 리뷰한다. 게이트는 자동 리뷰(trigger='auto')에만 적용된다."""
    rid = repo_repo.add(db, full_name="acme/api")  # trigger_mode 기본값 'auto'
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
    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
        worktree=fake_worktree,
        adapters=[
            FakeAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
            )
        ],
        prescreen=lambda diff, model: ("trivial", 0.1, "오타"),  # skip 판정
        repo_local_path="/tmp/x",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert review_repo.get_run(db, run_id)["status"] == "done"
    assert {f["vendor"] for f in finding_repo.list_for_run(db, run_id)} == {"claude"}


def _diff_block(path: str) -> str:
    return f"diff --git a/{path} b/{path}\n@@ -1 +1 @@\n-a\n+b\n"


def test_pipeline_chunks_large_diff_and_reviews_each_chunk(db, monkeypatch):
    """예산 초과 diff는 통째 취소 대신 파일 경계 청크로 나눠 벤더가 각 청크를 리뷰·집계한다."""
    from server.review.diff_filter import chunk_by_budget

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

    diff = _diff_block("a.py") + _diff_block("b.py") + _diff_block("c.py")
    budget = len(_diff_block("a.py")) + 1  # 블록 하나만 겨우 → 파일당 한 청크
    monkeypatch.setattr("server.pipeline.MAX_INLINE_DIFF_CHARS", budget)
    expected = len(chunk_by_budget(diff, budget))
    assert expected == 3

    class CountingAdapter:
        def __init__(self, vendor):
            self.vendor = vendor
            self.calls = 0

        async def review(self, *, prompt, **kw):
            self.calls += 1
            return [
                Finding(
                    self.vendor, f"f{self.calls}.py", 1, "high", "bug", "c", "r", 0.8
                )
            ]

    cap = CountingAdapter("claude")
    deps = PipelineDeps(
        gh_diff=lambda repo, n: diff,
        worktree=fake_worktree,
        adapters=[cap],
        prescreen=lambda diff, model: ("complex", 1.0, "big"),
        repo_local_path="/tmp/x",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))

    assert cap.calls == expected  # 청크마다 한 번씩 호출
    assert review_repo.get_run(db, run_id)["status"] == "done"
    assert len(finding_repo.list_for_run(db, run_id)) == expected  # 청크별 finding 집계
    vrs = review_repo.list_vendor_results(db, run_id)
    assert all(v["status"] == "done" for v in vrs)


def test_pipeline_cancels_when_only_noise_files_changed(db):
    """노이즈(lockfile 등)만 바뀐 PR → 필터 후 빈 diff → 벤더 호출 없이 canceled."""
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=14,
        title="lock",
        author="a",
        head_sha="locksha",
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

    noise = _diff_block("package-lock.json") + _diff_block("yarn.lock")
    deps = PipelineDeps(
        gh_diff=lambda repo, n: noise,
        worktree=blocked_worktree,
        adapters=[BlockedAdapter()],
        prescreen=lambda diff, model: (_ for _ in ()).throw(
            AssertionError("prescreen should not run on empty diff")
        ),
        repo_local_path="/tmp/x",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    run = review_repo.get_run(db, run_id)
    assert run["status"] == "canceled"
    assert "리뷰할 변경이 없습니다" in run["error"]
    assert review_repo.list_vendor_results(db, run_id) == []


def test_pipeline_filters_noise_keeps_real_files(db):
    """노이즈+실파일 혼합 diff → 프롬프트엔 실파일만 인라인된다."""
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=15,
        title="mix",
        author="a",
        head_sha="mixsha",
        base_ref="main",
        url="u",
    )
    diff = _diff_block("src/real.py") + _diff_block("package-lock.json")
    cap = PromptCapturingAdapter()
    deps = PipelineDeps(
        gh_diff=lambda repo, n: diff,
        worktree=fake_worktree,
        adapters=[cap],
        prescreen=lambda diff, model: ("complex", 0.9, "x"),
        repo_local_path="/tmp/x",
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert "src/real.py" in cap.prompt
    assert "package-lock.json" not in cap.prompt


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


def test_pipeline_degrades_when_context_gather_times_out(db, monkeypatch):
    import json
    import time

    monkeypatch.setattr("server.pipeline.CONTEXT_GATHER_TIMEOUT_SEC", 0.01)
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=13,
        title="t",
        author="a",
        head_sha="s13",
        base_ref="main",
        url="u",
    )

    class SlowContext:
        def gather(self, *, req):
            time.sleep(0.05)
            return "too late"

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[FakeAdapter("claude", [])],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/x",
        context=SlowContext(),
    )

    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    run = review_repo.get_run(db, run_id)
    assert run["status"] == "done" and run["context_text"] == ""
    assert json.loads(run["context_meta"])["degraded"] is True


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


def test_pipeline_populates_changed_files_from_diff(db):
    _, pid = _seed_pr(db, number=74, head_sha="s74")

    class SpyCtx:
        def __init__(self):
            self.results = []
            self.captured_req = None

        def gather(self, *, req):
            self.captured_req = req
            return ""

    spy = SpyCtx()
    diff = (
        "diff --git a/src/models/user.py b/src/models/user.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/db/schema.sql b/db/schema.sql\n@@ -1 +1 @@\n-c\n+d\n"
    )
    deps = PipelineDeps(
        gh_diff=lambda repo, n: diff,
        worktree=fake_worktree,
        adapters=[FakeAdapter("claude", [])],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/x",
        context=spy,
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert spy.captured_req.changed_files == ("src/models/user.py", "db/schema.sql")


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


class FakeVerify:
    def __init__(self, verdicts):
        self._verdicts = verdicts
        self.calls = []

    async def __call__(self, targets, ctx):
        self.calls.append((list(targets), ctx))
        return self._verdicts[: len(targets)]


def _seed_pr(db, *, number, head_sha, merge_enabled=0, verify_singles_on=None):
    rid = repo_repo.add(db, full_name="acme/api")
    fields = {"merge_enabled": merge_enabled}
    if verify_singles_on is not None:
        fields["verify_singles_on"] = verify_singles_on
    repo_repo.update(db, rid, **fields)
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=number,
        title="t",
        author="a",
        head_sha=head_sha,
        base_ref="main",
        url="u",
    )
    return rid, pid


def test_pipeline_verifies_high_severity_single_and_halves_confidence_on_refute(db):
    from server.review.verify import Verdict

    _, pid = _seed_pr(db, number=50, head_sha="s50", verify_singles_on=1)
    verify = FakeVerify([Verdict(refuted=True, rationale="오탐")])
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
        verify=verify,
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))

    # 고위험 SINGLE 하나만 검증 대상
    assert len(verify.calls) == 1
    targets = verify.calls[0][0]
    assert len(targets) == 1 and targets[0].file == "a.py"

    fs = {f["file"]: f for f in finding_repo.list_for_run(db, run_id)}
    assert fs["a.py"]["verify_status"] == "refuted"
    assert fs["a.py"]["verify_rationale"] == "오탐"
    assert fs["a.py"]["confidence"] == 0.4  # 0.8 * 0.5
    # 저위험은 검증 대상 아님 → verdict 미부착, confidence 유지
    assert fs["b.py"]["verify_status"] is None
    assert fs["b.py"]["confidence"] == 0.4


def test_pipeline_verify_confirmed_keeps_confidence(db):
    from server.review.verify import Verdict

    _, pid = _seed_pr(db, number=51, head_sha="s51", verify_singles_on=1)
    verify = FakeVerify([Verdict(refuted=False, rationale="실제 버그")])
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[
            FakeAdapter(
                "claude",
                [Finding("claude", "a.py", 1, "critical", "bug", "c", "r", 0.9)],
            )
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
        verify=verify,
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    f = finding_repo.list_for_run(db, run_id)[0]
    assert f["verify_status"] == "confirmed"
    assert f["confidence"] == 0.9


def test_pipeline_verify_skips_consensus_findings(db):
    from server.review.verify import Verdict

    _, pid = _seed_pr(db, number=52, head_sha="s52", verify_singles_on=1)
    verify = FakeVerify([Verdict(refuted=True, rationale="x")])
    # 양 벤더가 같은 위치·카테고리 → CONSENSUS → 검증 대상 아님
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[
            FakeAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
            ),
            FakeAdapter(
                "codex", [Finding("codex", "a.py", 2, "high", "bug", "c2", "r2", 0.7)]
            ),
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
        verify=verify,
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert verify.calls[0][0] == [] if verify.calls else True
    for f in finding_repo.list_for_run(db, run_id):
        assert f["verify_status"] is None


def test_pipeline_verify_disabled_by_default_not_called(db):
    from server.review.verify import Verdict

    _, pid = _seed_pr(db, number=53, head_sha="s53")  # verify_singles_on 미설정=off
    verify = FakeVerify([Verdict(refuted=True, rationale="x")])
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[
            FakeAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
            )
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
        verify=verify,
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert verify.calls == []
    f = finding_repo.list_for_run(db, run_id)[0]
    assert f["verify_status"] is None and f["confidence"] == 0.8


def test_pipeline_verify_degrades_when_verifier_raises(db):
    _, pid = _seed_pr(db, number=54, head_sha="s54", verify_singles_on=1)

    async def boom(targets, ctx):
        raise RuntimeError("verifier down")

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[
            FakeAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
            )
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
        verify=boom,
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    run = review_repo.get_run(db, run_id)
    assert run["status"] == "done"  # 검증 실패가 리뷰를 막지 않음
    f = finding_repo.list_for_run(db, run_id)[0]
    assert f["verify_status"] is None and f["confidence"] == 0.8


class PromptCapturingAdapter:
    vendor = "claude"

    def __init__(self):
        self.prompt = None

    async def review(self, *, prompt, **kw):
        self.prompt = prompt
        return []


def _incremental_deps(cap, *, full="FULL-DIFF", compare=None):
    return PipelineDeps(
        gh_diff=lambda repo, n: full,
        gh_compare_diff=compare,
        worktree=fake_worktree,
        adapters=[cap],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
    )


def _prior_done_run(db, pid, sha):
    from server.repos import review_repo

    r = review_repo.create_run(
        db, pr_id=pid, head_sha=sha, trigger="auto", effort="medium"
    )
    review_repo.finish_run(db, r, "done")


def test_incremental_uses_compare_diff_delta_and_records_base_sha(db):
    from server.repos import review_repo

    rid, pid = _seed_pr(db, number=60, head_sha="head2")
    repo_repo.update(db, rid, incremental_review_on=1)
    _prior_done_run(db, pid, "base1")
    cap = PromptCapturingAdapter()
    seen = {}
    deps = _incremental_deps(
        cap,
        compare=lambda repo, base, head: seen.update(base=base, head=head)
        or "DELTA-DIFF",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert seen == {"base": "base1", "head": "head2"}
    assert "DELTA-DIFF" in cap.prompt and "FULL-DIFF" not in cap.prompt
    assert review_repo.get_run(db, run_id)["base_sha"] == "base1"


def test_incremental_falls_back_to_full_when_no_prior_done_run(db):
    from server.repos import review_repo

    rid, pid = _seed_pr(db, number=61, head_sha="head2")
    repo_repo.update(db, rid, incremental_review_on=1)
    cap = PromptCapturingAdapter()
    deps = _incremental_deps(
        cap, compare=lambda *a: (_ for _ in ()).throw(AssertionError("불필요 호출"))
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert "FULL-DIFF" in cap.prompt
    assert review_repo.get_run(db, run_id)["base_sha"] is None


def test_incremental_empty_delta_falls_back_to_full(db):
    from server.repos import review_repo

    rid, pid = _seed_pr(db, number=62, head_sha="head2")
    repo_repo.update(db, rid, incremental_review_on=1)
    _prior_done_run(db, pid, "base1")
    cap = PromptCapturingAdapter()
    deps = _incremental_deps(cap, compare=lambda repo, base, head: "   \n")
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert "FULL-DIFF" in cap.prompt
    assert review_repo.get_run(db, run_id)["base_sha"] is None


def test_incremental_compare_error_degrades_to_full(db):
    from server.repos import review_repo

    rid, pid = _seed_pr(db, number=63, head_sha="head2")
    repo_repo.update(db, rid, incremental_review_on=1)
    _prior_done_run(db, pid, "base1")

    def boom(repo, base, head):
        raise RuntimeError("compare 404")

    cap = PromptCapturingAdapter()
    deps = _incremental_deps(cap, compare=boom)
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert "FULL-DIFF" in cap.prompt
    assert review_repo.get_run(db, run_id)["status"] == "done"  # 리뷰 차단 안 함
    assert review_repo.get_run(db, run_id)["base_sha"] is None


def test_incremental_off_override_uses_full_even_with_prior_run(db):
    from server.repos import review_repo

    rid, pid = _seed_pr(db, number=64, head_sha="head2")
    repo_repo.update(
        db, rid, incremental_review_on=0
    )  # per-repo 명시 off(전역 상속 무시)
    _prior_done_run(db, pid, "base1")
    calls = []
    cap = PromptCapturingAdapter()
    deps = _incremental_deps(
        cap, compare=lambda repo, base, head: calls.append((base, head)) or "DELTA"
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert calls == []  # off면 compare 자체를 호출하지 않음(degrade 아님)
    assert "FULL-DIFF" in cap.prompt
    assert review_repo.get_run(db, run_id)["base_sha"] is None


def test_incremental_on_by_default_uses_delta(db):
    """전역 기본 ON — repo override 없이도 직전 완료 런 이후 델타를 리뷰한다."""
    from server.repos import review_repo

    _, pid = _seed_pr(
        db, number=65, head_sha="head2"
    )  # incremental override 미설정=상속
    _prior_done_run(db, pid, "base1")
    cap = PromptCapturingAdapter()
    deps = _incremental_deps(cap, compare=lambda repo, base, head: "DELTA-DIFF")
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert "DELTA-DIFF" in cap.prompt and "FULL-DIFF" not in cap.prompt
    assert review_repo.get_run(db, run_id)["base_sha"] == "base1"


def test_pipeline_injects_repo_vendor_model_effort_into_harness(db):
    """레포별 모델/effort를 지정하면 전역 기본값을 덮어써 각 벤더 하네스에 반영된다."""
    rid, pid = _seed_pr(db, number=70, head_sha="s70")
    repo_repo.update(
        db,
        rid,
        claude_model="opus",
        claude_effort="xhigh",
        codex_model="gpt-5.4",
        codex_effort="high",
    )

    class HarnessCapturingAdapter:
        def __init__(self, vendor):
            self.vendor = vendor
            self.hp = None

        async def review(self, *, harness, **kw):
            self.hp = harness
            return []

    claude, codex = HarnessCapturingAdapter("claude"), HarnessCapturingAdapter("codex")
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[claude, codex],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert (claude.hp.model, claude.hp.effort) == ("opus", "xhigh")
    assert (codex.hp.codex_model, codex.hp.codex_effort) == ("gpt-5.4", "high")


def test_pipeline_falls_back_to_default_model_effort_when_repo_unset(db):
    """레포에 모델/effort 미설정(NULL)이면 코드 기본값으로 폴백."""
    _, pid = _seed_pr(db, number=76, head_sha="s76")

    class HarnessCapturingAdapter:
        vendor = "claude"

        def __init__(self):
            self.hp = None

        async def review(self, *, harness, **kw):
            self.hp = harness
            return []

    cap = HarnessCapturingAdapter()
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[cap],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert cap.hp.model == "sonnet" and cap.hp.effort == "medium"


def test_repo_inherits_global_model_effort_when_unset(db):
    """레포 모델/effort 미설정(NULL)이면 전역 기본값(app_settings)을 상속한다."""
    from server.repos import settings_repo

    settings_repo.update(
        db, review_model="opus", default_effort="high", codex_model="gpt-5.6"
    )
    _, pid = _seed_pr(db, number=88, head_sha="s88")  # 레포 모델/effort 미설정=상속

    class Cap:
        def __init__(self, vendor):
            self.vendor, self.hp = vendor, None

        async def review(self, *, harness, **kw):
            self.hp = harness
            return []

    claude, codex = Cap("claude"), Cap("codex")
    deps = PipelineDeps(
        gh_diff=lambda r, n: "diff...",
        worktree=fake_worktree,
        adapters=[claude, codex],
        prescreen=lambda d, m: ("complex", 0.9, "x"),
        repo_local_path="/tmp/x",
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert (claude.hp.model, claude.hp.effort) == ("opus", "high")
    assert (codex.hp.codex_model, codex.hp.codex_effort) == ("gpt-5.6", "high")


def test_global_effort_splits_per_vendor(db):
    """전역 effort를 벤더별로 분리하면(app_settings.claude_effort/codex_effort) 각 벤더에
    독립 적용된다. 한쪽만 설정하면 다른 벤더는 공용 default_effort로 폴백한다."""
    from server.repos import settings_repo

    settings_repo.update(db, default_effort="low", claude_effort="max")
    _, pid = _seed_pr(db, number=90, head_sha="s90")  # 레포 effort 미설정=전역 상속

    class Cap:
        def __init__(self, vendor):
            self.vendor, self.hp = vendor, None

        async def review(self, *, harness, **kw):
            self.hp = harness
            return []

    claude, codex = Cap("claude"), Cap("codex")
    deps = PipelineDeps(
        gh_diff=lambda r, n: "diff...",
        worktree=fake_worktree,
        adapters=[claude, codex],
        prescreen=lambda d, m: ("complex", 0.9, "x"),
        repo_local_path="/tmp/x",
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert claude.hp.effort == "max"  # 전역 claude_effort
    assert codex.hp.codex_effort == "low"  # codex_effort 미설정 → default_effort 폴백


def test_empty_prescreen_model_falls_back_to_default(db):
    """전역 prescreen_model이 ''(자유입력 콤보박스로 비워짐)이면 코드 기본값으로 폴백해
    빈 --model로 prescreen CLI를 400으로 깨뜨리지 않는다."""
    from server.repos import settings_repo

    settings_repo.update(db, prescreen_model="")
    _, pid = _seed_pr(db, number=91, head_sha="s91")
    seen = {}

    class Cap:
        vendor = "claude"

        async def review(self, **kw):
            return []

    def prescreen(diff, model):
        seen["model"] = model
        return ("complex", 0.9, "x")

    deps = PipelineDeps(
        gh_diff=lambda r, n: "diff...",
        worktree=fake_worktree,
        adapters=[Cap()],
        prescreen=prescreen,
        repo_local_path="/tmp/x",
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert seen["model"] == "haiku"  # config.DEFAULT_PRESCREEN_MODEL


def test_prescreen_reused_across_reviews_of_identical_diff(db):
    _, pid = _seed_pr(db, number=71, head_sha="s71")

    class CountingPrescreen:
        def __init__(self):
            self.calls = 0

        def __call__(self, diff, model):
            self.calls += 1
            return ("complex", 0.9, "핵심 로직")

    counter = CountingPrescreen()
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "SAME-DIFF",
        worktree=fake_worktree,
        adapters=[FakeAdapter("claude", [])],
        prescreen=counter,
        repo_local_path="/tmp/acme-api",
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert counter.calls == 1  # 두 번째는 동일 diff → prescreen 재사용


def test_prescreen_not_reused_when_diff_changes(db):
    _, pid = _seed_pr(db, number=72, head_sha="s72")

    class CountingPrescreen:
        def __init__(self):
            self.calls = 0
            self.diff = "DIFF-A"

        def __call__(self, diff, model):
            self.calls += 1
            return ("complex", 0.9, "핵심")

    counter = CountingPrescreen()
    diff_box = {"v": "DIFF-A"}
    deps = PipelineDeps(
        gh_diff=lambda repo, n: diff_box["v"],
        worktree=fake_worktree,
        adapters=[FakeAdapter("claude", [])],
        prescreen=counter,
        repo_local_path="/tmp/acme-api",
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    diff_box["v"] = "DIFF-B"  # diff 변경 → 캐시 미스
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert counter.calls == 2


def test_prescreen_fallback_not_cached_so_next_run_retries(db):
    _, pid = _seed_pr(db, number=73, head_sha="s73")

    class CountingPrescreen:
        def __init__(self):
            self.calls = 0

        def __call__(self, diff, model):
            self.calls += 1
            return ("moderate", 0.5, PRESCREEN_FALLBACK_REASON)

    counter = CountingPrescreen()
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "SAME-DIFF",
        worktree=fake_worktree,
        adapters=[FakeAdapter("claude", [])],
        prescreen=counter,
        repo_local_path="/tmp/acme-api",
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert counter.calls == 2  # 파싱 실패는 캐시 안 됨 → 재시도(self-heal)


class _CodexFail:
    vendor = "codex"

    async def review(self, **kw):
        raise RuntimeError("boom")


class _ClaudeMustNotRun:
    vendor = "claude"

    async def review(self, **kw):
        raise AssertionError("이미 성공한 벤더는 재실행되면 안 됨")


def _partial_fail_run(db, *, pid, claude_finding, repo_path="/tmp/x"):
    """claude 성공 + codex 실패한 부분 실패 run을 만들고 run_id 반환."""
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[FakeAdapter("claude", [claude_finding]), _CodexFail()],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path=repo_path,
    )
    return asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))


def _retry_deps(codex_findings, repo_path="/tmp/x"):
    return PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[_ClaudeMustNotRun(), FakeAdapter("codex", codex_findings)],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path=repo_path,
    )


def test_retry_reruns_only_failed_vendor_into_same_run(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=20,
        title="t",
        author="a",
        head_sha="s20",
        base_ref="main",
        url="u",
    )
    run_id = _partial_fail_run(
        db,
        pid=pid,
        claude_finding=Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8),
    )
    assert review_repo.failed_vendors(db, run_id) == ["codex"]

    retry_run_id = asyncio.run(
        retry_pr(
            db,
            pr_id=pid,
            run_id=run_id,
            deps=_retry_deps(
                [Finding("codex", "b.py", 2, "low", "style", "c2", "r2", 0.4)]
            ),
        )
    )
    assert retry_run_id == run_id  # 같은 run 재사용
    n_runs = db.execute(
        "SELECT COUNT(*) c FROM review_run WHERE pr_id=?", (pid,)
    ).fetchone()["c"]
    assert n_runs == 1  # 새 run 미생성
    vr = {v["vendor"]: v["status"] for v in review_repo.list_vendor_results(db, run_id)}
    assert vr == {"claude": "done", "codex": "done"}  # 실패 벤더가 done으로 채워짐
    fs = {f["vendor"]: f for f in finding_repo.list_for_run(db, run_id)}
    assert set(fs) == {"claude", "codex"}
    assert fs["claude"]["file"] == "a.py" and fs["codex"]["file"] == "b.py"


def test_retry_retags_consensus_when_merge_enabled(db):
    _, pid = _seed_pr(db, number=21, head_sha="s21", merge_enabled=1)
    run_id = _partial_fail_run(
        db,
        pid=pid,
        claude_finding=Finding("claude", "a.py", 10, "high", "bug", "c", "r", 0.8),
    )
    assert finding_repo.list_for_run(db, run_id)[0]["consensus"] == "single"

    asyncio.run(
        retry_pr(
            db,
            pr_id=pid,
            run_id=run_id,
            deps=_retry_deps(
                [Finding("codex", "a.py", 11, "high", "bug", "c2", "r2", 0.7)]
            ),
        )
    )
    fs = finding_repo.list_for_run(db, run_id)
    assert len(fs) == 2
    assert all(f["consensus"] == "consensus" for f in fs)  # union 재태깅
    assert len({f["consensus_group_id"] for f in fs}) == 1


def test_retry_preserves_human_decision_on_retag(db):
    _, pid = _seed_pr(db, number=22, head_sha="s22", merge_enabled=1)
    run_id = _partial_fail_run(
        db,
        pid=pid,
        claude_finding=Finding("claude", "a.py", 10, "high", "bug", "c", "r", 0.8),
    )
    claude_fid = finding_repo.list_for_run(db, run_id)[0]["id"]
    finding_repo.set_status(db, claude_fid, "approved")

    asyncio.run(
        retry_pr(
            db,
            pr_id=pid,
            run_id=run_id,
            deps=_retry_deps(
                [Finding("codex", "a.py", 11, "high", "bug", "c2", "r2", 0.7)]
            ),
        )
    )
    row = finding_repo.get(db, claude_fid)
    assert row["status"] == "approved"  # consensus UPDATE이 사람 결정을 지우지 않음
    assert row["consensus"] == "consensus"  # 태깅은 갱신


def test_retry_targets_validated_run_not_latest(db):
    # 같은 head에 done run이 둘일 때 retry는 전달된 run_id만 대상으로 한다(latest 아님).
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
    target_run = _partial_fail_run(  # run A: codex 실패(재시도 대상)
        db,
        pid=pid,
        claude_finding=Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8),
    )
    # run B: 이후 생긴 최신 done run(두 벤더 성공) — latest지만 재시도 대상 아님
    later_run = asyncio.run(
        review_pr(
            db,
            pr_id=pid,
            trigger="manual",
            deps=PipelineDeps(
                gh_diff=lambda repo, n: "diff...",
                worktree=fake_worktree,
                adapters=[
                    FakeAdapter(
                        "claude",
                        [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)],
                    ),
                    FakeAdapter(
                        "codex",
                        [Finding("codex", "z.py", 9, "low", "style", "c", "r", 0.4)],
                    ),
                ],
                prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
                repo_local_path="/tmp/x",
            ),
        )
    )
    assert later_run != target_run and review_repo.failed_vendors(db, later_run) == []

    asyncio.run(
        retry_pr(
            db,
            pr_id=pid,
            run_id=target_run,
            deps=_retry_deps(
                [Finding("codex", "b.py", 2, "low", "style", "c2", "r2", 0.4)]
            ),
        )
    )
    # codex finding은 검증된 target_run에 들어가고 later_run은 그대로
    assert {f["vendor"] for f in finding_repo.list_for_run(db, target_run)} == {
        "claude",
        "codex",
    }
    assert review_repo.failed_vendors(db, target_run) == []


def test_retry_bails_when_head_advanced_between_enqueue_and_run(db):
    # 엔드포인트 검증 후 poller가 head를 전진시킨 경우(TOCTOU): retry는 새 head diff를
    # 옛 run에 섞지 않고 PipelineError로 취소하며 데이터를 건드리지 않는다.
    _, pid = _seed_pr(db, number=24, head_sha="s24", merge_enabled=1)
    run_id = _partial_fail_run(
        db,
        pid=pid,
        claude_finding=Finding("claude", "a.py", 10, "high", "bug", "c", "r", 0.8),
    )
    before = [dict(f) for f in finding_repo.list_for_run(db, run_id)]
    pr_repo.upsert(  # poller가 head를 전진시킴(같은 pr, 새 sha)
        db,
        repo_id=pr_repo.get(db, pid)["repo_id"],
        number=24,
        title="t",
        author="a",
        head_sha="s24-NEW",
        base_ref="main",
        url="u",
    )

    with pytest.raises(PipelineError):
        asyncio.run(
            retry_pr(
                db,
                pr_id=pid,
                run_id=run_id,
                deps=_retry_deps(
                    [Finding("codex", "b.py", 2, "low", "style", "c2", "r2", 0.4)]
                ),
            )
        )
    # run은 오염되지 않음: finding 그대로, codex 여전히 failed
    assert [dict(f) for f in finding_repo.list_for_run(db, run_id)] == before
    assert review_repo.failed_vendors(db, run_id) == ["codex"]


def test_retry_vendor_refail_stays_failed_and_retryable(db):
    # 재시도한 벤더가 또 실패하면 'running'이 아니라 'failed'로 남아 다음 재시도가 가능하다.
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=25,
        title="t",
        author="a",
        head_sha="s25",
        base_ref="main",
        url="u",
    )
    run_id = _partial_fail_run(
        db,
        pid=pid,
        claude_finding=Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8),
    )
    # codex가 재시도에서도 실패(_CodexFail 재사용)
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[_ClaudeMustNotRun(), _CodexFail()],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )
    asyncio.run(retry_pr(db, pr_id=pid, run_id=run_id, deps=deps))
    vr = {v["vendor"]: v["status"] for v in review_repo.list_vendor_results(db, run_id)}
    assert vr["codex"] == "failed"  # 'running' 고착 아님
    assert review_repo.failed_vendors(db, run_id) == ["codex"]  # 재시도 가능 상태 유지


def test_build_prompt_empty_no_block():
    out = _build_prompt({"number": 3, "title": "T", "author": "u"}, "DIFF", "")
    assert "## 외부 컨텍스트" not in out and "DIFF" in out


def test_build_prompt_with_block():
    out = _build_prompt({"number": 3, "title": "T", "author": "u"}, "DIFF", "### s\nhi")
    assert "## 외부 컨텍스트" in out and "hi" in out
