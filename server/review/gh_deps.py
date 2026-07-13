import tempfile

from server.context.registry import build_context_provider
from server.github.gh import GhClient
from server.pipeline import PipelineDeps
from server.review.harness import HarnessProfile
from server.review.prescreen import prescreen
from server.review.vendors import ClaudeAdapter, CodexAdapter
from server.review.verify import make_verifier
from server.review.worktree import prepared_worktree


def build_deps(repo, settings) -> PipelineDeps:
    if not repo["local_path"]:
        raise ValueError(f"repo {repo['full_name']}에 local_path 미설정")
    gh = GhClient()
    hp = HarnessProfile.load(repo["harness_name"])

    def _prescreen_tuple(diff, model):
        # ★개정: prescreen도 격리 config dir + 인증 주입(전역 미상속 유지).
        with tempfile.TemporaryDirectory(prefix="almighty-ps-") as rt:
            hp.prepare_runtime(runtime_dir=rt)
            r = prescreen(
                diff=diff, model=model, env=hp.isolated_env(runtime_dir=rt), cwd=rt
            )
        return (r.complexity, r.score, r.reason)

    adapters = [ClaudeAdapter(), CodexAdapter()]
    return PipelineDeps(
        gh_diff=gh.diff,
        worktree=prepared_worktree,
        adapters=adapters,
        prescreen=_prescreen_tuple,
        repo_local_path=repo["local_path"],
        context=build_context_provider(repo, settings),
        verify=make_verifier(adapters, prepared_worktree),
    )
