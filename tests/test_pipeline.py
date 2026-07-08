import asyncio
from contextlib import contextmanager

import pytest

from server.models import Finding
from server.pipeline import review_pr, PipelineDeps, PipelineError
from server.repos import repo_repo, pr_repo, finding_repo, review_repo


@contextmanager
def fake_worktree(repo, sha):
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
