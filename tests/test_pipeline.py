import asyncio
import json
from contextlib import contextmanager

import pytest

from server.models import Finding
from server.pipeline import (
    review_pr,
    retry_pr,
    PipelineDeps,
    PipelineError,
    PipelineStaleHead,
    VendorRunResult,
    _build_prompt,
    _context_budget,
    _group_duplicate_candidates,
    _policy_mode,
    _run_vendor,
    _safe_concurrency_limit,
)
from server.repos import (
    finding_repo,
    pr_repo,
    repo_repo,
    review_repo,
    settings_repo,
)
from server.review.finding_policy import policy_snapshot_from_row
from server.review.harness import HarnessProfile, RuntimeCredentialError
from server.review.prescreen import PRESCREEN_FALLBACK_REASON
from server.review.vendor_telemetry import EXECUTION_IDENTITY_FIELDS
from server.review.vendors import CodexAdapter


def test_runtime_cleanup_failure_cannot_report_vendor_success(tmp_path):
    class Harness:
        @contextmanager
        def runtime_credentials(self, **kwargs):
            yield
            raise RuntimeCredentialError("runtime_cleanup_failed")

    class Adapter:
        vendor = "codex"

        async def review(self, **kwargs):
            return []

    class Pool:
        async def run(self, job):
            return await job()

    result = asyncio.run(
        _run_vendor(
            Adapter(), ["prompt"], pool=Pool(), wt=tmp_path,
            hp=Harness(), rt=str(tmp_path / "runtime"),
        )
    )

    assert result.status == "failed"
    assert result.findings == []
    assert result.chunks[0]["safe_error_code"] == "runtime_cleanup_failed"


def test_review_run_policy_snapshot_is_immutable_after_config_change(db, monkeypatch):
    _, pid = _seed_pr(db, number=90, head_sha="policy-snapshot")
    monkeypatch.setattr("server.config.REVIEW_POLICY_ENFORCEMENT_UNLOCKED", True)
    monkeypatch.setattr("server.config.REVIEW_SCOPE_GUARD_MODE", "enforce")
    monkeypatch.setattr(
        "server.config.REVIEW_SCOPE_ENFORCE_REPOS", frozenset({"acme/api"})
    )
    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
        worktree=fake_worktree,
        adapters=[FakeAdapter("claude", [])],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )

    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    before = policy_snapshot_from_row(review_repo.get_run(db, run_id))
    assert before is not None
    assert before.scope.effective_mode == "enforce"

    monkeypatch.setattr("server.config.REVIEW_POLICY_ENFORCEMENT_UNLOCKED", False)
    after = policy_snapshot_from_row(review_repo.get_run(db, run_id))
    assert after == before


def test_safe_concurrency_limit_recovers_legacy_invalid_settings():
    assert _safe_concurrency_limit(1) == 1
    assert _safe_concurrency_limit(8) == 8
    assert _safe_concurrency_limit(0) == 2
    assert _safe_concurrency_limit(9) == 2
    assert _safe_concurrency_limit(True) == 2


def test_enforcement_requires_global_canary_and_honors_kill_switch(monkeypatch):
    repo = {
        "full_name": "acme/api",
        "review_scope_guard_mode": None,
        "review_dedupe_mode": None,
    }
    monkeypatch.setattr("server.config.REVIEW_SCOPE_ENFORCE_REPOS", frozenset())
    monkeypatch.setattr("server.config.REVIEW_POLICY_ENFORCEMENT_UNLOCKED", False)
    repo["review_scope_guard_mode"] = "enforce"
    assert _policy_mode(
        repo, "review_scope_guard_mode", "observe", policy="scope"
    ) == "observe"
    repo["review_scope_guard_mode"] = None
    monkeypatch.setattr("server.config.REVIEW_POLICY_ENFORCEMENT_UNLOCKED", True)
    monkeypatch.setattr("server.config.REVIEW_SCOPE_KILL_SWITCH", False)
    assert _policy_mode(
        repo, "review_scope_guard_mode", "enforce", policy="scope"
    ) == "observe"

    monkeypatch.setattr(
        "server.config.REVIEW_SCOPE_ENFORCE_REPOS", frozenset({"acme/api"})
    )
    assert _policy_mode(
        repo, "review_scope_guard_mode", "enforce", policy="scope"
    ) == "enforce"

    monkeypatch.setattr("server.config.REVIEW_SCOPE_KILL_SWITCH", True)
    repo["review_scope_guard_mode"] = "enforce"
    assert _policy_mode(
        repo, "review_scope_guard_mode", "observe", policy="scope"
    ) == "observe"


def test_context_budget_caps_repeated_prompt_and_vendor_cost():
    assert _context_budget(1, 1) == 20_000
    assert _context_budget(2, 1) == 10_000
    assert _context_budget(1, 2) == 10_000
    assert _context_budget(10, 2) == 1_000


@pytest.fixture(autouse=True)
def _no_runtime_credentials(monkeypatch):
    monkeypatch.setattr(
        "server.review.harness.HarnessProfile.prepare_runtime",
        lambda self, runtime_dir, vendor: None,
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


def test_pipeline_discards_findings_when_head_changes_during_vendor_review(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=81,
        title="t",
        author="a",
        head_sha="old",
        base_ref="main",
        url="u",
    )

    class HeadChangingAdapter(FakeAdapter):
        async def review(self, **kw):
            db.execute("UPDATE pull_request SET head_sha='new' WHERE id=?", (pid,))
            db.commit()
            return self._f

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[
            HeadChangingAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.9)]
            )
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
    )

    with pytest.raises(PipelineStaleHead):
        asyncio.run(
            review_pr(
                db,
                pr_id=pid,
                trigger="manual",
                deps=deps,
            )
        )

    run = db.execute(
        "SELECT * FROM review_run WHERE pr_id=? ORDER BY id DESC LIMIT 1", (pid,)
    ).fetchone()
    assert run["status"] == "canceled"
    assert finding_repo.list_for_run(db, run["id"]) == []
    vendor = review_repo.list_vendor_results(db, run["id"])[0]
    assert vendor["status"] == "canceled"
    assert vendor["execution_meta"] is not None
    assert pr_repo.get(db, pid)["last_reviewed_sha"] is None


def test_pipeline_runs_vendor_in_injected_plain_snapshot(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=82, title="t", author="a", head_sha="s82",
        base_ref="main", url="u",
    )
    seen = {}

    @contextmanager
    def snapshot(worktree):
        seen["source"] = str(worktree)
        yield "/tmp/plain-snapshot"

    class SnapshotAdapter:
        vendor = "claude"

        async def review(self, *, workdir, **kwargs):
            seen["cwd"] = str(workdir)
            return []

    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
        worktree=fake_worktree,
        adapters=[SnapshotAdapter()],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/source",
        snapshot=snapshot,
    )

    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))

    assert seen == {"source": "/tmp/fake-wt", "cwd": "/tmp/plain-snapshot"}


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


def test_pipeline_marks_vendor_partial_and_persists_chunk_coverage(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=12, title="partial", author="a",
        head_sha="partial-sha", base_ref="main", url="u",
    )
    diff = _diff_block("a.py") + _diff_block("b.py")
    monkeypatch.setattr(
        "server.pipeline.MAX_INLINE_DIFF_CHARS", len(_diff_block("a.py")) + 1
    )

    class PartialAdapter:
        vendor = "claude"

        def __init__(self):
            self.calls = 0

        async def review(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("provider body must not persist")
            return [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]

    adapter = PartialAdapter()
    deps = PipelineDeps(
        gh_diff=lambda repo, n: diff,
        worktree=fake_worktree,
        adapters=[adapter],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )

    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))

    assert review_repo.get_run(db, run_id)["status"] == "done"
    vendor = review_repo.list_vendor_results(db, run_id)[0]
    assert vendor["status"] == "partial"
    assert vendor["error"] == "partial chunk failure"
    chunks = vendor["execution_meta"]["attempts"][0]["chunks"]
    assert [chunk["status"] for chunk in chunks] == ["done", "failed"]
    assert len(finding_repo.list_for_run(db, run_id)) == 1
    assert review_repo.failed_vendors(db, run_id) == ["claude"]
    assert "provider body" not in str(vendor)


def test_partial_retry_runs_only_unresolved_chunk_and_appends_attempt(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=13, title="partial", author="a",
        head_sha="partial-retry", base_ref="main", url="u",
    )
    a, b = _diff_block("a.py"), _diff_block("b.py")
    diff = a + b
    monkeypatch.setattr("server.pipeline.MAX_INLINE_DIFF_CHARS", len(a) + 1)

    class FirstPass:
        vendor = "claude"

        def __init__(self):
            self.calls = 0

        async def review(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("temporary")
            return [Finding("claude", "a.py", 1, "high", "bug", "first", "r", 0.8)]

    first = FirstPass()
    initial_deps = PipelineDeps(
        gh_diff=lambda repo, n: diff,
        worktree=fake_worktree,
        adapters=[first],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )
    run_id = asyncio.run(
        review_pr(db, pr_id=pid, trigger="manual", deps=initial_deps)
    )

    class RetryPass:
        vendor = "claude"

        def __init__(self):
            self.prompts = []

        async def review(self, *, prompt, **kwargs):
            self.prompts.append(prompt)
            return [Finding("claude", "b.py", 1, "medium", "bug", "second", "r", 0.7)]

    retry = RetryPass()
    retry_deps = PipelineDeps(
        gh_diff=lambda repo, n: diff,
        worktree=fake_worktree,
        adapters=[retry],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )

    asyncio.run(retry_pr(db, pr_id=pid, run_id=run_id, deps=retry_deps))

    assert len(retry.prompts) == 1
    assert "b.py" in retry.prompts[0] and "a.py" not in retry.prompts[0].split("## Diff", 1)[1]
    vendor = review_repo.list_vendor_results(db, run_id)[0]
    assert vendor["status"] == "done"
    attempts = vendor["execution_meta"]["attempts"]
    assert [attempt["attempt"] for attempt in attempts] == [1, 2]
    assert [chunk["index"] for chunk in attempts[1]["chunks"]] == [1]
    assert {f["claim"] for f in finding_repo.list_for_run(db, run_id)} == {"first", "second"}


def test_retry_groups_duplicate_against_prior_successful_chunk(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=131, title="retry-dupe", author="a",
        head_sha="retry-dupe", base_ref="main", url="u",
    )
    a, b = _diff_block("a.py"), _diff_block("b.py")
    diff = a + b
    monkeypatch.setattr("server.pipeline.MAX_INLINE_DIFF_CHARS", len(a) + 1)

    class Initial:
        vendor = "claude"
        calls = 0

        async def review(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("temporary")
            return [Finding(
                "claude", "b.py", 1, "high", "bug", "same defect", "r", 0.8
            )]

    initial_deps = PipelineDeps(
        gh_diff=lambda repo, n: diff,
        worktree=fake_worktree,
        adapters=[Initial()],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=initial_deps))

    class Retry:
        vendor = "claude"

        async def review(self, **kwargs):
            return [Finding(
                "claude", "b.py", 1, "high", "bug", " same   defect! ", "r", 0.8
            )]

    retry_deps = PipelineDeps(
        gh_diff=lambda repo, n: diff,
        worktree=fake_worktree,
        adapters=[Retry()],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )

    asyncio.run(retry_pr(db, pr_id=pid, run_id=run_id, deps=retry_deps))
    findings = finding_repo.list_for_run(db, run_id)

    assert len(findings) == 2
    assert {item["duplicate_group_id"] for item in findings} == {1}
    assert all(item["duplicate_suggested"] for item in findings)


def test_scope_policy_reassigns_other_chunk_and_marks_context_line_unpostable(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=14, title="scope", author="a",
        head_sha="scope-sha", base_ref="main", url="u",
    )
    a = _diff_block("a.py")
    b = "diff --git a/b.py b/b.py\n@@ -1,2 +1,2 @@\n context\n-old\n+new\n"
    diff = a + b
    monkeypatch.setattr(
        "server.pipeline.MAX_INLINE_DIFF_CHARS", max(len(a), len(b)) + 1
    )
    monkeypatch.setattr("server.config.REVIEW_SCOPE_GUARD_MODE", "observe")

    class ScopeAdapter:
        vendor = "claude"

        def __init__(self):
            self.calls = 0

        async def review(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return [
                    Finding("claude", "b.py", 2, "high", "bug", "cross", "r", 0.8),
                    Finding("claude", "b.py", 1, "low", "bug", "context", "r", 0.5),
                ]
            return []

    deps = PipelineDeps(
        gh_diff=lambda repo, n: diff,
        worktree=fake_worktree,
        adapters=[ScopeAdapter()],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )

    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    findings = {finding["claim"]: finding for finding in finding_repo.list_for_run(db, run_id)}

    assert findings["cross"]["source_chunk_index"] == 0
    assert findings["cross"]["owner_chunk_index"] == 1
    assert findings["cross"]["scope_status"] == "reassigned"
    assert findings["cross"]["posting_eligible"] == 1
    assert findings["context"]["scope_status"] == "would_reject"
    assert findings["context"]["posting_eligible"] == 0
    chunk = review_repo.list_vendor_results(db, run_id)[0]["execution_meta"]["attempts"][0]["chunks"][0]
    assert chunk["scope_reassigned"] == 1 and chunk["scope_rejected"] == 1


def test_duplicate_policy_groups_exact_claim_without_merging_distinct_claims(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=15, title="dupe", author="a",
        head_sha="dupe-sha", base_ref="main", url="u",
    )
    a, b = _diff_block("a.py"), _diff_block("b.py")
    diff = a + b
    monkeypatch.setattr("server.pipeline.MAX_INLINE_DIFF_CHARS", len(a) + 1)
    monkeypatch.setattr("server.config.REVIEW_DEDUPE_MODE", "observe")

    class DuplicateAdapter:
        vendor = "claude"

        def __init__(self):
            self.calls = 0

        async def review(self, **kwargs):
            self.calls += 1
            claim = "Same defect!" if self.calls == 1 else " same   defect "
            return [
                Finding("claude", "b.py", 1, "high", "bug", claim, "r", 0.9),
                Finding("claude", "b.py", 1, "high", "bug", f"distinct {self.calls}", "r", 0.9),
            ]

    deps = PipelineDeps(
        gh_diff=lambda repo, n: diff,
        worktree=fake_worktree,
        adapters=[DuplicateAdapter()],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )

    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    findings = finding_repo.list_for_run(db, run_id)
    duplicate = [finding for finding in findings if "same" in finding["claim"].lower()]
    distinct = [finding for finding in findings if finding["claim"].startswith("distinct")]

    assert len(duplicate) == 2
    assert {finding["duplicate_group_id"] for finding in duplicate} == {1}
    assert all(finding["duplicate_suggested"] for finding in duplicate)
    assert all(finding["posting_eligible"] for finding in duplicate)
    assert all(finding["duplicate_group_id"] is None for finding in distinct)


def test_duplicate_policy_does_not_group_same_claim_on_different_lines():
    first = Finding("claude", "a.py", 10, "high", "bug", "same claim", "r", 0.9)
    second = Finding("claude", "a.py", 20, "high", "bug", "same claim", "r", 0.9)
    first.owner_chunk_index = second.owner_chunk_index = 0
    first.source_chunk_index = second.source_chunk_index = 0
    result = VendorRunResult(
        vendor="claude", status="done", findings=[first, second], duration_ms=0,
        chunks=[{"index": 0, "duplicate_groups": 0}],
    )

    _group_duplicate_candidates([result], mode="enforce")

    assert first.duplicate_group_id is None
    assert second.duplicate_group_id is None
    assert first.posting_eligible is True and second.posting_eligible is True


def test_pipeline_selects_and_persists_context_per_chunk(db, monkeypatch):
    from server.context.base import ContextBlock, ContextResult

    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=16, title="ctx", author="a",
        head_sha="ctx-sha", base_ref="main", url="u",
    )
    a, b = _diff_block("a.py"), _diff_block("b.py")
    diff = a + b
    monkeypatch.setattr("server.pipeline.MAX_INLINE_DIFF_CHARS", len(a) + 1)
    monkeypatch.setattr("server.config.MAX_CONTEXT_CHARS_TOTAL", 700)

    class ChunkContext:
        def __init__(self):
            self.results = [ContextResult(
                provider="static", status="ok", text="legacy", blocks=(
                    ContextBlock(
                        "static", "a-rule", "A-CONTEXT-" * 8, 50, True,
                        relevant_files=("a.py",),
                    ),
                    ContextBlock(
                        "static", "b-rule", "B-CONTEXT-" * 8, 50, True,
                        relevant_files=("b.py",),
                    ),
                )
            )]

        def gather(self, *, req):
            return "legacy"

    class CaptureAdapter:
        vendor = "claude"

        def __init__(self):
            self.prompts = []

        async def review(self, *, prompt, **kwargs):
            self.prompts.append(prompt)
            return []

    adapter = CaptureAdapter()
    deps = PipelineDeps(
        gh_diff=lambda repo, n: diff,
        worktree=fake_worktree,
        adapters=[adapter],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
        context=ChunkContext(),
    )

    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    stored = review_repo.get_context_chunks(review_repo.get_run(db, run_id))

    assert len(adapter.prompts) == len(stored) == 2
    assert "A-CONTEXT" in adapter.prompts[0] and "B-CONTEXT" not in adapter.prompts[0]
    assert "B-CONTEXT" in adapter.prompts[1] and "A-CONTEXT" not in adapter.prompts[1]
    assert stored[0]["context_hash"] != stored[1]["context_hash"]
    assert all(item["manifest"] for item in stored)


def test_pipeline_verifies_each_finding_with_owner_chunk_only(db, monkeypatch):
    from server.review.verify import Verdict

    _, pid = _seed_pr(db, number=58, head_sha="s58", verify_singles_on=1)
    a, b = _diff_block("a.py"), _diff_block("b.py")
    monkeypatch.setattr("server.pipeline.MAX_INLINE_DIFF_CHARS", len(a) + 1)

    class ChunkAdapter:
        vendor = "claude"

        async def review(self, *, prompt, **kwargs):
            path = "a.py" if "diff --git a/a.py" in prompt else "b.py"
            return [Finding("claude", path, 1, "high", "bug", path, "r", 0.9)]

    verify = FakeVerify([
        Verdict(refuted=False, independent=True, evidence_status="independent_model_support"),
        Verdict(refuted=False, independent=True, evidence_status="independent_model_support"),
    ])
    deps = PipelineDeps(
        gh_diff=lambda repo, n: a + b,
        worktree=fake_worktree,
        adapters=[ChunkAdapter()],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/acme-api",
        verify=verify,
    )

    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))

    assert len(verify.calls) == 2
    assert "diff --git a/a.py" in verify.calls[0][1].diff
    assert "diff --git a/b.py" not in verify.calls[0][1].diff
    assert "diff --git a/b.py" in verify.calls[1][1].diff
    assert "diff --git a/a.py" not in verify.calls[1][1].diff


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
    run = review_repo.get_run(db, ei.value.run_id)
    assert run["status"] == "failed"
    assert "rate limit" not in str(ei.value)  # provider 원문은 영속/전파하지 않음
    assert run["error"] == "all vendors failed → claude:failed:unknown; codex:failed:unknown"


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
    from server.context.base import ContextBlock, ContextResult

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
                ContextResult(
                    provider="static",
                    status="ok",
                    text="hello ctx",
                    meta={
                        "failed_sources": ["graphql"],
                        "items_read": 8,
                        "items_selected": 6,
                        "automated_items_selected": 2,
                    },
                    blocks=(ContextBlock(
                        source="static",
                        block_id="safe-test-context",
                        text="hello ctx",
                        priority=10,
                        recoverable_from_repo=False,
                        retention="review_history",
                    ),),
                )
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
    assert meta["diff_chars"] == len("diff...")
    assert meta["context_chars"] == len("hello ctx")
    assert meta["prompt_count"] == 1 and meta["vendor_count"] == 1
    assert meta["repeated_context_chars"] == meta["chunk_context_chars"]
    chunk_contexts = review_repo.get_context_chunks(run)
    assert len(chunk_contexts) == 1
    assert "hello ctx" in chunk_contexts[0]["text"]
    assert chunk_contexts[0]["manifest"][0]["selected"] is True
    assert meta["sources"][0]["failed_sources"] == ["graphql"]
    assert meta["sources"][0]["items_read"] == 8
    assert meta["sources"][0]["items_selected"] == 6
    assert meta["sources"][0]["automated_items_selected"] == 2


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


def test_pipeline_injects_static_context_from_base_revision(db, tmp_path):
    """참조문서는 PR head에서 바뀌어도 worktree가 가리키는 base revision 버전을 주입한다."""
    import json
    import subprocess
    from server.repos import settings_repo
    from server.context.registry import build_context_provider

    clone_dir = tmp_path / "clone"  # local_path(=구 컨텍스트 root) — ctx.md 없음
    clone_dir.mkdir()
    wt_dir = tmp_path / "wt"
    wt_dir.mkdir()
    (wt_dir / "ctx.md").write_text("base 아키텍처 결정: 큐 기반")
    subprocess.run(["git", "-C", str(wt_dir), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(wt_dir), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(wt_dir), "config", "user.name", "Test"], check=True
    )
    subprocess.run(["git", "-C", str(wt_dir), "add", "ctx.md"], check=True)
    subprocess.run(
        ["git", "-C", str(wt_dir), "commit", "-qm", "base"], check=True
    )
    subprocess.run(["git", "-C", str(wt_dir), "branch", "-M", "main"], check=True)
    base_sha = subprocess.check_output(
        ["git", "-C", str(wt_dir), "rev-parse", "HEAD"], text=True
    ).strip()
    (wt_dir / "ctx.md").write_text("PR이 바꾼 지침")
    subprocess.run(["git", "-C", str(wt_dir), "add", "ctx.md"], check=True)
    subprocess.run(
        ["git", "-C", str(wt_dir), "commit", "-qm", "move main"], check=True
    )

    rid = repo_repo.add(db, full_name="acme/api", local_path=str(clone_dir))
    repo_repo.update(
        db,
        rid,
        context_static_on=1,
        static_context_path="ctx.md",  # 상대경로(UI 저장형)
    )
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=40,
        title="t",
        author="a",
        head_sha="s40",
        base_ref="main",
        base_sha=base_sha,
        url="u",
    )
    repo = repo_repo.get(db, rid)
    settings = settings_repo.get(db)

    @contextmanager
    def wt_worktree(repo, sha, pr_number=None):
        yield str(wt_dir)

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
        worktree=wt_worktree,
        adapters=[cap],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path=str(clone_dir),
        context=build_context_provider(repo, settings),
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))

    assert "## 외부 컨텍스트" in cap.prompt and "base 아키텍처 결정" in cap.prompt
    assert "PR이 바꾼 지침" not in cap.prompt
    run = review_repo.get_run(db, run_id)
    assert run["context_text"] == ""  # manifest-only repository text is prompt-only
    assert review_repo.get_context_chunks(run)[0]["text"] == ""
    meta = json.loads(run["context_meta"])
    assert meta["context_payload_persisted"] is False
    assert (
        meta["sources"][0]["provider"] == "static"
        and meta["sources"][0]["status"] == "ok"
    )


def test_pipeline_does_not_persist_sensitive_or_manifest_only_context(db):
    import hashlib
    import json
    from server.context.base import ContextBlock, ContextResult

    rid = repo_repo.add(db, full_name="acme/private")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=22, title="t", author="a",
        head_sha="s22", base_ref="main", url="u",
    )

    class SensitiveContext:
        def __init__(self):
            self.results = [ContextResult(
                provider="db_schema",
                status="ok",
                text="secret schema",
                blocks=(ContextBlock(
                    source="db_schema",
                    block_id="schema",
                    text="secret schema",
                    priority=10,
                    recoverable_from_repo=False,
                    sensitivity="sensitive",
                    retention="manifest_only",
                ),),
            )]

        def gather(self, *, req):
            return "secret schema"

    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
        worktree=fake_worktree,
        adapters=[FakeAdapter("claude", [])],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
        context=SensitiveContext(),
    )

    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    run = review_repo.get_run(db, run_id)
    chunks = review_repo.get_context_chunks(run)
    meta = json.loads(run["context_meta"])

    assert run["context_text"] == ""
    assert chunks[0]["text"] == ""
    assert chunks[0]["context_hash"] != hashlib.sha256(b"").hexdigest()
    assert meta["context_payload_persisted"] is False
    assert meta["chunk_contexts"][0]["payload_persisted"] is False
    assert "secret schema" not in run["context_meta"]


def test_pipeline_sets_context_request_workdir_to_worktree(db):
    """ContextRequest.workdir가 열린 worktree 경로로 채워진다(파일 provider 봉쇄 root)."""
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=41,
        title="t",
        author="a",
        head_sha="s41",
        base_ref="main",
        url="u",
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
    assert spy.captured_req.workdir == "/tmp/fake-wt"


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
        gh_diff=lambda repo, n: _diff_block("a.py"),
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
    verify = FakeVerify([
        Verdict(
            refuted=False,
            rationale="실제 버그",
            independent=True,
            evidence_status="independent_model_support",
        )
    ])
    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
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
    assert f["verify_independent"] == 1
    assert f["verify_evidence_status"] == "independent_model_support"
    assert f["confidence"] == 0.9


def test_pipeline_same_vendor_support_is_not_confirmed(db):
    from server.review.vendor_telemetry import unavailable_meta
    from server.review.vendors import VendorExecution
    from server.review.verify import Verdict

    _, pid = _seed_pr(db, number=57, head_sha="s57", verify_singles_on=1)
    execution = VendorExecution(
        output="",
        status="done",
        safe_error_code=None,
        exit_code=0,
        cli_name="claude",
        cli_version="2.1.198",
        event_schema="claude-stream-json-v1",
        stream_truncated=False,
        telemetry=unavailable_meta("claude", status="done"),
        duration_ms=7,
    )
    verify = FakeVerify([
        Verdict(
            refuted=False,
            rationale="자체 재검토",
            independent=False,
            evidence_status="supported_self",
            execution_attempts=(("claude", execution),),
        )
    ])
    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
        worktree=fake_worktree,
        adapters=[FakeAdapter(
            "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.9)]
        )],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/acme-api",
        verify=verify,
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    f = finding_repo.list_for_run(db, run_id)[0]
    assert f["verify_status"] == "supported_self"
    assert f["verify_independent"] == 0
    assert f["verify_evidence_status"] == "supported_self"
    vendor = review_repo.list_vendor_results(db, run_id)[0]
    execution_meta = vendor["execution_meta"]
    assert [attempt["phase"] for attempt in execution_meta["attempts"]] == [
        "review", "verify"
    ]


def test_pipeline_verify_contested_keeps_confidence(db):
    from server.review.verify import Verdict

    _, pid = _seed_pr(db, number=55, head_sha="s55", verify_singles_on=1)
    # 반박당했지만 저자가 방어 → contested: 진짜 finding이므로 신뢰도를 반감하지 않는다.
    verify = FakeVerify(
        [Verdict(refuted=False, contested=True, rationale="반박 vs 변호")]
    )
    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
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
    assert f["verify_status"] == "contested"
    assert f["confidence"] == 0.9  # 반감하지 않음


def test_pipeline_verify_skips_consensus_findings(db):
    from server.review.verify import Verdict

    _, pid = _seed_pr(db, number=52, head_sha="s52", verify_singles_on=1)
    verify = FakeVerify([Verdict(refuted=True, rationale="x")])
    # 양 벤더가 같은 위치·카테고리 → CONSENSUS → 검증 대상 아님
    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
        worktree=fake_worktree,
        adapters=[
            FakeAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
            ),
            FakeAdapter(
                "codex", [Finding("codex", "a.py", 1, "high", "bug", "c2", "r2", 0.7)]
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


def test_pipeline_does_not_verify_out_of_scope_high_finding(db):
    from server.review.verify import Verdict

    _, pid = _seed_pr(db, number=59, head_sha="s59", verify_singles_on=1)
    verify = FakeVerify([Verdict(refuted=False)])
    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
        worktree=fake_worktree,
        adapters=[FakeAdapter(
            "claude", [Finding("claude", "outside.py", 1, "high", "bug", "c", "r", 0.9)]
        )],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/acme-api",
        verify=verify,
    )

    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))

    assert verify.calls == []
    finding = finding_repo.list_for_run(db, run_id)[0]
    assert finding["scope_status"] == "would_reject"
    assert finding["verify_status"] is None


def test_pipeline_verify_disabled_by_default_not_called(db):
    from server.review.verify import Verdict

    _, pid = _seed_pr(db, number=53, head_sha="s53")  # verify_singles_on 미설정=off
    verify = FakeVerify([Verdict(refuted=True, rationale="x")])
    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
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


def test_pipeline_verify_degraded_verdict_leaves_finding_unlabeled(db):
    # 검증이 실행되지 못한 finding(벤더 1개뿐·refuter 실패)은 confirmed로
    # 오라벨하지 않고 미검증 상태 그대로 둔다.
    from server.review.verify import Verdict

    _, pid = _seed_pr(db, number=56, head_sha="s56", verify_singles_on=1)
    verify = FakeVerify([Verdict(refuted=False, degraded=True)])
    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
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
    assert f["verify_status"] == "degraded"
    assert f["verify_rationale"] is None
    assert f["verify_independent"] == 0
    assert f["verify_evidence_status"] == "unverified"
    assert f["confidence"] == 0.9


def test_pipeline_verify_degrades_when_verifier_raises(db):
    _, pid = _seed_pr(db, number=54, head_sha="s54", verify_singles_on=1)

    async def boom(targets, ctx):
        raise RuntimeError("verifier down")

    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
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
    assert run["status"] == "done"  # 일반 검증 실패는 리뷰를 막지 않음
    f = finding_repo.list_for_run(db, run_id)[0]
    assert f["verify_status"] == "degraded" and f["confidence"] == 0.8
    assert f["verify_evidence_status"] == "degraded"


def test_pipeline_verify_exception_with_cleanup_note_is_not_absorbed_as_done(db):
    from server.review.snapshot import ReviewSnapshotCleanupError

    _, pid = _seed_pr(db, number=57, head_sha="s57", verify_singles_on=1)

    async def cleanup_failed(targets, ctx):
        error = RuntimeError("verify failed")
        error.add_note("snapshot_cleanup_failed")
        raise error

    deps = PipelineDeps(
        gh_diff=lambda repo, n: _diff_block("a.py"),
        worktree=fake_worktree,
        adapters=[
            FakeAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
            )
        ],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
        verify=cleanup_failed,
    )

    with pytest.raises(PipelineError) as caught:
        asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert isinstance(caught.value.__cause__, ReviewSnapshotCleanupError)
    run = db.execute(
        "SELECT * FROM review_run WHERE pr_id=? ORDER BY id DESC LIMIT 1", (pid,)
    ).fetchone()
    assert run["status"] != "done"


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


def test_worker_review_uses_base_to_expected_head_exact_diff(db):
    from server.repos import review_repo

    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=65,
        title="t",
        author="a",
        head_sha="head2",
        base_ref="main",
        base_sha="base0",
        url="u",
    )
    cap = PromptCapturingAdapter()
    seen = {}

    def compare(repo, base, head):
        seen.update(repo=repo, base=base, head=head)
        return "EXACT-DIFF"

    deps = PipelineDeps(
        gh_diff=lambda *args: (_ for _ in ()).throw(
            AssertionError("strict worker review must not use live PR diff")
        ),
        gh_compare_diff=compare,
        worktree=fake_worktree,
        adapters=[cap],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
    )
    run_id = asyncio.run(
        review_pr(
            db,
            pr_id=pid,
            trigger="manual",
            deps=deps,
            expected_head_sha="head2",
        )
    )

    assert seen == {"repo": "acme/api", "base": "base0", "head": "head2"}
    assert "EXACT-DIFF" in cap.prompt
    assert review_repo.get_run(db, run_id)["head_sha"] == "head2"


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


def test_non_claude_prescreen_model_falls_back_and_records_reason(db):
    from server.repos import settings_repo

    settings_repo.update(db, prescreen_model="gpt-5.6-terra")
    _, pid = _seed_pr(db, number=92, head_sha="s92")
    seen = {"models": []}

    class Cap:
        vendor = "claude"

        async def review(self, **kw):
            return []

    def prescreen(diff, model):
        seen["models"].append(model)
        return ("complex", 0.9, "핵심 로직")

    deps = PipelineDeps(
        gh_diff=lambda r, n: "diff...",
        worktree=fake_worktree,
        adapters=[Cap()],
        prescreen=prescreen,
        repo_local_path="/tmp/x",
    )
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))

    row = db.execute(
        "SELECT model, reason, diff_hash FROM pre_screen "
        "WHERE pr_id=? ORDER BY id DESC LIMIT 1",
        (pid,),
    ).fetchone()
    assert seen["models"] == ["haiku"]
    assert row["model"] == "haiku"
    assert row["reason"].startswith("non_claude_model_fallback;")
    assert row["diff_hash"] is None  # 잘못된 설정 사유를 정상 모델 cache에 섞지 않는다.

    settings_repo.update(db, prescreen_model="haiku")
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    corrected = db.execute(
        "SELECT reason FROM pre_screen WHERE pr_id=? ORDER BY id DESC LIMIT 1",
        (pid,),
    ).fetchone()
    assert seen["models"] == ["haiku", "haiku"]
    assert not corrected["reason"].startswith("non_claude_model_fallback;")


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


def test_prescreen_subprocess_failure_degrades_to_review(db):
    # 사전평가 CLI 실패(subprocess 에러)는 최적화 게이트 실패일 뿐 — 리뷰는 진행된다.
    _, pid = _seed_pr(db, number=74, head_sha="s74")

    class BoomOncePrescreen:
        def __init__(self):
            self.calls = 0

        def __call__(self, diff, model):
            self.calls += 1
            raise RuntimeError("claude CLI unavailable")

    boom = BoomOncePrescreen()
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "SAME-DIFF",
        worktree=fake_worktree,
        adapters=[
            FakeAdapter(
                "claude", [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
            )
        ],
        prescreen=boom,
        repo_local_path="/tmp/acme-api",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    run = review_repo.get_run(db, run_id)
    assert run["status"] == "done"  # CLI 실패가 run을 죽이지 않음
    assert len(finding_repo.list_for_run(db, run_id)) == 1
    ps = db.execute(
        "SELECT reason, diff_hash FROM pre_screen ORDER BY id DESC"
    ).fetchone()
    assert "사전평가 CLI 실패" in ps["reason"]
    assert ps["diff_hash"] is None  # 비결정적 실패 → 캐시 미등록
    # 같은 diff 재실행 → 캐시 재사용 없이 CLI 재시도(self-heal)
    asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert boom.calls == 2


class _CodexFail:
    vendor = "codex"

    async def review(self, **kw):
        raise RuntimeError("boom")


class _ClaudeMustNotRun:
    vendor = "claude"

    async def review(self, **kw):
        raise AssertionError("이미 성공한 벤더는 재실행되면 안 됨")


def _partial_fail_run(
    db, *, pid, claude_finding, repo_path="/tmp/x", diff_text="diff..."
):
    """claude 성공 + codex 실패한 부분 실패 run을 만들고 run_id 반환."""
    settings_repo.update(db, codex_model="test-codex")
    deps = PipelineDeps(
        gh_diff=lambda repo, n: diff_text,
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


_RETRY_IDENTITY_TARGETS = (
    ("root", "schema_version"),
    *(("root", key) for key in EXECUTION_IDENTITY_FIELDS),
    ("chunk", "chunk_hash"),
    ("chunk", "context_hash"),
    ("chunk", "chunker_version"),
    ("chunk", "prompt_nonce"),
)


@pytest.mark.parametrize("location,key", _RETRY_IDENTITY_TARGETS)
@pytest.mark.parametrize("mutation", ("mismatch", "absence"))
def test_retry_identity_is_complete_and_fail_closed_before_runner(
    db, location, key, mutation
):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=200, title="identity", author="a",
        head_sha="identity", base_ref="main", url="u",
    )
    run_id = _partial_fail_run(
        db,
        pid=pid,
        claude_finding=Finding(
            "claude", "a.py", 1, "high", "bug", "c", "r", 0.8
        ),
    )
    row = db.execute(
        "SELECT id, execution_meta FROM vendor_result WHERE run_id=? AND vendor='codex'",
        (run_id,),
    ).fetchone()
    meta = json.loads(row["execution_meta"])
    target = (
        meta if location == "root"
        else meta["attempts"][0]["chunks"][0]
    )
    if mutation == "absence":
        target.pop(key)
    elif key in {
        "prompt_hash", "harness_config_hash", "adapter_config_hash", "diff_hash",
        "context_hash", "policy_decision_hash", "policy_config_hash", "chunk_hash",
    }:
        target[key] = "f" * 64 if target[key] != "f" * 64 else "e" * 64
    elif key in {"scope_policy_mode", "dedupe_policy_mode"}:
        target[key] = "enforce" if target[key] == "observe" else "observe"
    elif key == "schema_version":
        target[key] = 999
    elif key == "vendor":
        target[key] = "claude"
    elif key == "prompt_nonce":
        target[key] = "deadbeef" if target[key] != "deadbeef" else "feedface"
    else:
        target[key] = f"{target[key]}-changed"
    db.execute(
        "UPDATE vendor_result SET execution_meta=? WHERE id=?",
        (json.dumps(meta, separators=(",", ":"), sort_keys=True), row["id"]),
    )
    db.commit()

    class NoCall:
        vendor = "codex"
        calls = 0

        async def review(self, **kwargs):
            self.calls += 1
            raise AssertionError("retry runner must not be called")

    adapter = NoCall()
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[adapter],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )
    with pytest.raises(PipelineError, match="new_full_run_required"):
        asyncio.run(retry_pr(db, pr_id=pid, run_id=run_id, deps=deps))
    assert adapter.calls == 0


@pytest.mark.parametrize(
    "drift",
    ("model", "effort", "harness", "adapter", "cli", "event_schema", "diff", "prompt"),
)
def test_retry_current_execution_drift_fails_before_runner(db, monkeypatch, drift):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=202, title="identity", author="a",
        head_sha="identity-current", base_ref="main", url="u",
    )
    run_id = _partial_fail_run(
        db,
        pid=pid,
        claude_finding=Finding(
            "claude", "a.py", 1, "high", "bug", "c", "r", 0.8
        ),
    )

    class NoCall:
        vendor = "codex"
        calls = 0

        async def review(self, **kwargs):
            self.calls += 1
            raise AssertionError("retry runner must not be called")

    adapter = NoCall()
    current_diff = "diff..."
    if drift == "model":
        settings_repo.update(db, codex_model="changed-codex")
    elif drift == "effort":
        settings_repo.update(db, codex_effort="high")
    elif drift == "harness":
        profile = HarnessProfile.load("default")
        profile.codex_sandbox = "danger-full-access"
        monkeypatch.setattr(
            "server.pipeline.HarnessProfile.load", lambda name: profile
        )
    elif drift == "adapter":
        adapter.adapter_version = "injected-v2"
    elif drift == "cli":
        adapter.cli_version = "injected-v2"
    elif drift == "event_schema":
        adapter.event_schema_version = "injected-schema-v2"
    elif drift == "diff":
        current_diff = "changed diff..."
    elif drift == "prompt":
        db.execute("UPDATE pull_request SET title='changed title' WHERE id=?", (pid,))
        db.commit()

    deps = PipelineDeps(
        gh_diff=lambda repo, n: current_diff,
        worktree=fake_worktree,
        adapters=[adapter],
        prescreen=lambda d, m: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )
    with pytest.raises(PipelineError, match="new_full_run_required"):
        asyncio.run(retry_pr(db, pr_id=pid, run_id=run_id, deps=deps))
    assert adapter.calls == 0


@pytest.mark.parametrize("drift", ("schema_hint", "review_argv"))
def test_retry_binds_actual_builtin_wire_inputs_before_runner(
    db, monkeypatch, drift
):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=203, title="wire", author="a",
        head_sha="wire", base_ref="main", url="u",
    )
    settings_repo.update(db, codex_model="test-codex")
    monkeypatch.setattr(
        "server.review.vendors._cli_version",
        lambda vendor: "codex-cli 0.144.5",
    )

    async def invalid_output_runner(*args, **kwargs):
        return "not structured findings"

    initial = CodexAdapter(runner=invalid_output_runner)
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...", worktree=fake_worktree,
        adapters=[FakeAdapter("claude", []), initial],
        prescreen=lambda d, m: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert review_repo.failed_vendors(db, run_id) == ["codex"]

    if drift == "schema_hint":
        monkeypatch.setattr(
            "server.review.vendors.PROMPT_SCHEMA_HINT", "changed schema hint"
        )
    else:
        original = CodexAdapter._build_review_argv

        def changed_argv(
            self, hp, *, last_message_path, cli_version=None
        ):
            return [
                *original(
                    self, hp, last_message_path=last_message_path,
                    cli_version=cli_version,
                ),
                "--changed-review-option",
            ]

        monkeypatch.setattr(CodexAdapter, "_build_review_argv", changed_argv)

    calls = 0

    async def must_not_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("retry runner must not be called")

    retry_adapter = CodexAdapter(runner=must_not_run)
    retry_deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...", worktree=fake_worktree,
        adapters=[retry_adapter],
        prescreen=lambda d, m: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )
    with pytest.raises(PipelineError, match="new_full_run_required"):
        asyncio.run(retry_pr(db, pr_id=pid, run_id=run_id, deps=retry_deps))
    assert calls == 0


def test_retry_reprobes_replaced_cli_before_vendor_runner(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=204, title="cli replacement", author="a",
        head_sha="cli-replace", base_ref="main", url="u",
    )
    settings_repo.update(db, codex_model="test-codex")
    current_version = ["codex-cli 0.144.5"]
    probes = 0

    def version_probe(vendor):
        nonlocal probes
        probes += 1
        return current_version[0]

    monkeypatch.setattr("server.review.vendors._cli_version", version_probe)

    async def invalid_output_runner(*args, **kwargs):
        return "not structured findings"

    initial = CodexAdapter(runner=invalid_output_runner)
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...", worktree=fake_worktree,
        adapters=[FakeAdapter("claude", []), initial],
        prescreen=lambda d, m: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert review_repo.failed_vendors(db, run_id) == ["codex"]
    probes_after_initial = probes

    current_version[0] = "codex-cli 0.145.0"
    calls = 0

    async def must_not_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("changed CLI retry must stop before runner")

    retry_deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...", worktree=fake_worktree,
        adapters=[CodexAdapter(runner=must_not_run)],
        prescreen=lambda d, m: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )
    with pytest.raises(PipelineError, match="new_full_run_required"):
        asyncio.run(retry_pr(db, pr_id=pid, run_id=run_id, deps=retry_deps))
    assert probes > probes_after_initial
    assert calls == 0


def test_cli_change_between_identity_and_invocation_stops_before_runner(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=205, title="pre-call drift", author="a",
        head_sha="pre-call", base_ref="main", url="u",
    )
    versions = iter(("codex-cli 0.144.5", "codex-cli 0.145.0"))
    calls = 0

    async def must_not_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("runner must not observe a changed CLI")

    adapter = CodexAdapter(runner=must_not_run)
    adapter.probe_cli_version = lambda: next(versions)
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...", worktree=fake_worktree,
        adapters=[adapter], prescreen=lambda d, m: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )

    with pytest.raises(PipelineError, match="new_full_run_required"):
        asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    assert calls == 0


def test_retry_policy_snapshot_drift_fails_before_runner(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db, repo_id=rid, number=201, title="policy", author="a",
        head_sha="policy", base_ref="main", url="u",
    )
    run_id = _partial_fail_run(
        db,
        pid=pid,
        claude_finding=Finding(
            "claude", "a.py", 1, "high", "bug", "c", "r", 0.8
        ),
    )

    class NoCall:
        vendor = "codex"
        calls = 0

        async def review(self, **kwargs):
            self.calls += 1

    adapter = NoCall()
    monkeypatch.setattr("server.config.REVIEW_DEDUPE_MODE", "enforce")
    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...", worktree=fake_worktree,
        adapters=[adapter], prescreen=lambda d, m: ("complex", 0.9, "핵심"),
        repo_local_path="/tmp/x",
    )
    with pytest.raises(PipelineError, match="new_full_run_required"):
        asyncio.run(retry_pr(db, pr_id=pid, run_id=run_id, deps=deps))
    assert adapter.calls == 0


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


def test_retry_rejects_target_that_is_no_longer_latest(db):
    # API 검증 뒤 같은 head의 새 run이 생겨도 worker가 과거 run을 뒤늦게 변경하지 않는다.
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

    with pytest.raises(PipelineError, match="과거 리뷰 run"):
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
    assert {f["vendor"] for f in finding_repo.list_for_run(db, target_run)} == {
        "claude",
    }
    assert review_repo.failed_vendors(db, target_run) == ["codex"]
    assert review_repo.failed_vendors(db, later_run) == []


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


def test_worker_retry_uses_base_to_expected_head_exact_diff(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=28,
        title="t",
        author="a",
        head_sha="s28",
        base_ref="main",
        base_sha="base28",
        url="u",
    )
    run_id = _partial_fail_run(
        db,
        pid=pid,
        claude_finding=Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8),
        diff_text="EXACT-RETRY-DIFF",
    )
    seen = {}
    deps = _retry_deps(
        [Finding("codex", "b.py", 2, "low", "style", "c2", "r2", 0.4)]
    )
    deps.gh_diff = lambda *args: (_ for _ in ()).throw(
        AssertionError("strict worker retry must not use live PR diff")
    )
    deps.gh_compare_diff = lambda repo, base, head: (
        seen.update(repo=repo, base=base, head=head) or "EXACT-RETRY-DIFF"
    )

    asyncio.run(
        retry_pr(
            db,
            pr_id=pid,
            run_id=run_id,
            deps=deps,
            expected_head_sha="s28",
        )
    )

    assert seen == {"repo": "acme/api", "base": "base28", "head": "s28"}
    assert review_repo.failed_vendors(db, run_id) == []


def test_retry_fails_if_failed_vendor_is_disabled_after_enqueue(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=26,
        title="t",
        author="a",
        head_sha="s26",
        base_ref="main",
        url="u",
    )
    run_id = _partial_fail_run(
        db,
        pid=pid,
        claude_finding=Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8),
    )
    repo_repo.update(db, rid, vendor_codex_on=0)

    with pytest.raises(PipelineError, match="new_full_run_required"):
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
    assert review_repo.failed_vendors(db, run_id) == ["codex"]


def test_retry_rolls_back_vendor_success_when_finding_persistence_fails(db, monkeypatch):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=27,
        title="t",
        author="a",
        head_sha="s27",
        base_ref="main",
        url="u",
    )
    run_id = _partial_fail_run(
        db,
        pid=pid,
        claude_finding=Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8),
    )

    def fail_add(*args, **kwargs):
        raise RuntimeError("persist boom")

    monkeypatch.setattr("server.pipeline.finding_repo.add", fail_add)
    with pytest.raises(PipelineError, match="persist boom"):
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

    assert review_repo.failed_vendors(db, run_id) == ["codex"]
    assert {f["vendor"] for f in finding_repo.list_for_run(db, run_id)} == {"claude"}


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


def test_build_prompt_lists_only_added_lines_as_allowed_finding_locations():
    out = _build_prompt(
        {"number": 3, "title": "T", "author": "u"},
        "diff",
        "",
        owned_changed_lines={"src/a.py": frozenset({3, 4, 5, 9})},
    )

    assert '"src/a.py": 3-5,9' in out
    assert "허용되는 실제 추가 라인" in out
    assert "허용 목록에 없는 위치" in out


def test_build_prompt_without_owned_lines_requires_empty_findings():
    out = _build_prompt(
        {"number": 3, "title": "T", "author": "u"}, "diff", "",
        owned_changed_lines={},
    )

    assert "추가된 RIGHT-side 라인 없음 — finding을 만들지 말 것" in out


def _seed_single_pr(db, number=7, sha="sha1"):
    rid = repo_repo.add(db, full_name="acme/api")
    return pr_repo.upsert(
        db,
        repo_id=rid,
        number=number,
        title="t",
        author="a",
        head_sha=sha,
        base_ref="main",
        url="u",
    )


def test_pipeline_does_not_store_successful_vendor_raw_output(db, tmp_path, monkeypatch):
    monkeypatch.setattr("server.config.RAW_DIR", tmp_path / "raw")
    pid = _seed_single_pr(db)

    class RawAdapter:
        vendor = "claude"

        async def review(self, *, raw_sink=None, **kw):
            assert raw_sink is None
            return [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[RawAdapter()],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
    )
    run_id = asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    vr = db.execute("SELECT * FROM vendor_result WHERE run_id=?", (run_id,)).fetchone()
    assert vr["raw_path"] is None
    assert not (tmp_path / "raw").exists()


def test_pipeline_does_not_store_raw_when_vendor_parse_fails(db, tmp_path, monkeypatch):
    monkeypatch.setattr("server.config.RAW_DIR", tmp_path / "raw")
    pid = _seed_single_pr(db)

    class BrokenAdapter:
        vendor = "claude"

        async def review(self, *, raw_sink=None, **kw):
            assert raw_sink is None
            raise ValueError("provider body SECRET must not persist")

    deps = PipelineDeps(
        gh_diff=lambda repo, n: "diff...",
        worktree=fake_worktree,
        adapters=[BrokenAdapter()],
        prescreen=lambda diff, model: ("complex", 0.9, "핵심 로직"),
        repo_local_path="/tmp/acme-api",
    )
    with pytest.raises(PipelineError):
        asyncio.run(review_pr(db, pr_id=pid, trigger="manual", deps=deps))
    vr = db.execute("SELECT * FROM vendor_result").fetchone()
    assert vr["status"] == "failed"
    assert vr["raw_path"] is None
    assert "SECRET" not in (vr["error"] or "")
    assert not (tmp_path / "raw").exists()
