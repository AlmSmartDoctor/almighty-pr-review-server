import tempfile

from server.context.registry import build_context_provider
from server.github.gh import GhClient
from server.review.harness import HarnessProfile
from server.review.pipeline_contracts import PipelineDeps, row_value
from server.review.prescreen import prescreen
from server.review.vendors import ClaudeAdapter, CodexAdapter
from server.review.snapshot import prepared_plain_snapshot
from server.review.verify import make_verifier
from server.review.worktree import prepared_worktree


def build_deps(repo, settings, *, pool=None, gh_factory=GhClient) -> PipelineDeps:
    # gh_factory injection keeps rehearsal/offline tests from constructing a native client.
    # local_path는 선택값 — 없으면 파이프라인이 gh.clone으로 서비스 전용 영구 clone을 사용.
    gh = gh_factory()
    hp = HarnessProfile.load(repo["harness_name"])

    def _prescreen_tuple(diff, model):
        # ★개정: prescreen도 격리 config dir + 인증 주입(전역 미상속 유지).
        with tempfile.TemporaryDirectory(prefix="almighty-ps-") as rt:
            with hp.runtime_credentials(runtime_dir=rt, vendor="claude"):
                r = prescreen(
                    diff=diff,
                    model=model,
                    env=hp.isolated_env(runtime_dir=rt),
                    cwd=rt,
                )
        return (r.complexity, r.score, r.reason)

    adapters = [ClaudeAdapter(), CodexAdapter()]
    # verify도 리뷰와 동일하게 레포 벤더 토글을 따른다 — OFF 벤더(미설치·미인증일 수
    # 있음)를 refuter로 exec하면 매번 실패해 검증이 조용히 무력화된다.
    verify_adapters = [
        ad
        for ad in adapters
        if row_value(repo, f"vendor_{ad.vendor}_on", 1)
    ]
    return PipelineDeps(
        gh_diff=gh.diff,
        gh_compare_diff=gh.compare_diff,
        worktree=prepared_worktree,
        adapters=adapters,
        prescreen=_prescreen_tuple,
        repo_local_path=repo["local_path"],
        clone=gh.clone,
        context=build_context_provider(repo, settings, gh=gh),
        pool=pool,
        verify=make_verifier(
            verify_adapters,
            prepared_worktree,
            gh.clone,
            snapshot=prepared_plain_snapshot,
        ),
        snapshot=prepared_plain_snapshot,
    )
