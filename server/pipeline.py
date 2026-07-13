import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from server.config import DEFAULT_EFFORT, CONTEXT_GATHER_TIMEOUT_SEC
from server.repos import (
    finding_repo,
    prescreen_repo,
    pr_repo,
    repo_repo,
    review_repo,
    settings_repo,
)
from server.review.harness import HarnessProfile
from server.review.merge import deterministic_merge
from server.review.prescreen import (
    MAX_INLINE_DIFF_CHARS,
    PreScreenResult,
    diff_too_large_reason,
)
from server.context.base import ContextRequest, redact_secrets
from server.review.runner import RunnerPool
from server.seams import NoOpContextProvider


class PipelineError(RuntimeError):
    """★개정(codex v3): 실패 시 어느 attempt run이 실패했는지 worker에 전달."""

    def __init__(self, run_id: int, message: str):
        super().__init__(message)
        self.run_id = run_id


@dataclass
class PipelineDeps:
    gh_diff: Callable[[str, int], str]
    worktree: Callable  # contextmanager(repo, sha, pr_number) -> path
    adapters: list  # vendor adapters (.vendor, async .review())
    prescreen: Callable[
        [str, str], tuple
    ]  # (diff, model) -> (complexity, score, reason)
    repo_local_path: str
    context: object = field(default_factory=NoOpContextProvider)
    pool: RunnerPool = None  # ★개정: 벤더 병렬 실행 세마포어(없으면 생성)


async def review_pr(conn, *, pr_id: int, trigger: str, deps: PipelineDeps) -> int:
    """run을 만들고 실행. ★개정: 예외 시 run을 failed로 마감 후 재던짐
    (review_run/review_job 상태 정합성). worker가 run_id를 몰라도 run은 스스로 정리됨."""
    pr = pr_repo.get(conn, pr_id)
    repo = repo_repo.get(conn, pr["repo_id"])
    settings = settings_repo.get(conn)
    run_id = review_repo.create_run(
        conn,
        pr_id=pr_id,
        head_sha=pr["head_sha"],
        trigger=trigger,
        effort=repo["default_effort"] or DEFAULT_EFFORT,
        merge_enabled=repo["merge_enabled"],
    )
    try:
        await _execute_run(
            conn, run_id=run_id, pr=pr, repo=repo, settings=settings, deps=deps
        )
    except Exception as e:
        review_repo.finish_run(conn, run_id, "failed", error=str(e))
        raise PipelineError(run_id, str(e)) from e  # ★개정: run_id 전달
    return run_id


async def _execute_run(conn, *, run_id, pr, repo, settings, deps) -> None:
    hp = HarnessProfile.load(repo["harness_name"])
    # ★ 설정에서 고른 벤더별 모델로 하네스 기본값 덮어씀
    hp.model = settings["review_model"]  # Claude
    hp.codex_model = settings["codex_model"]  # Codex ("" = codex 자체 기본)
    prescreen_model = settings["prescreen_model"]
    pool = deps.pool or RunnerPool(limit=settings["concurrency_limit"])

    # sync subprocess(gh/prescreen)를 to_thread로 오프로드 → 이벤트루프 비블록
    diff = await asyncio.to_thread(deps.gh_diff, repo["full_name"], pr["number"])

    # 2. Pre-screen
    complexity, score, reason = await asyncio.to_thread(
        deps.prescreen, diff, prescreen_model
    )
    ps = PreScreenResult(complexity, score, reason)
    decided = ps.decide(threshold=settings["prescreen_gate_threshold"])
    prescreen_repo.add(
        conn,
        pr_id=pr["id"],
        head_sha=pr["head_sha"],
        model=prescreen_model,
        complexity=complexity,
        score=score,
        reason=reason,
        duration_ms=0,
        decided=decided,
    )
    if decided == "skip" and repo["trigger_mode"] == "auto":
        review_repo.finish_run(conn, run_id, "canceled")
        pr_repo.mark_reviewed(conn, pr["id"], pr["head_sha"])
        return
    if len(diff) > MAX_INLINE_DIFF_CHARS:
        review_repo.finish_run(
            conn, run_id, "canceled", error=diff_too_large_reason(diff)
        )
        return

    # ★개정 (codex v5 [LOW]): enabled 벤더가 0개면 리뷰할 게 없다 → worktree도
    # 만들지 않고 canceled로 마감(reviewed로 오판하지 않음). trigger/설정 단계에서
    # 걸러지는 게 정상이나 방어적으로 여기서도 canceled 처리.
    adapters = _enabled_adapters(deps.adapters, repo)
    if not adapters:
        review_repo.finish_run(conn, run_id, "canceled", error="no vendor enabled")
        return

    # 컨텍스트 수집(B-INV-1: 부모 프로세스·게이트 통과 후·worktree 이전).
    # B-INV-8: to_thread+총 타임아웃, 실패/초과는 ''로 degrade → 리뷰 절대 차단 안 함.
    req = ContextRequest(
        repo=repo["full_name"],
        pr_number=pr["number"],
        title=pr["title"] or "",
        author=pr["author"] or "",
        head_ref=pr["head_ref"] if "head_ref" in pr.keys() else "",
        base_ref=pr["base_ref"] or "",
        body=pr["body"] if "body" in pr.keys() else "",
    )
    try:
        context_text = await asyncio.wait_for(
            asyncio.to_thread(deps.context.gather, req=req),
            timeout=CONTEXT_GATHER_TIMEOUT_SEC,
        )
    except Exception as e:  # B-INV-4: best-effort degrade — 침묵 금지, redact 후 로그
        context_text = ""
        print(
            f"[pipeline] context gather degraded: "
            f"{redact_secrets(f'{type(e).__name__}: {e}')}"
        )

    # 런당 외부 컨텍스트 감사 저장(원문 + 소스별 meta). r.error는 Composite에서 이미 redact.
    results = getattr(deps.context, "results", [])
    context_meta = {
        "sources": [
            {
                "provider": r.provider,
                "status": r.status,
                "chars": len(r.text or ""),
                "error": r.error,
            }
            for r in results
        ]
    }
    review_repo.set_context(conn, run_id, text=context_text, meta=context_meta)

    # 3. Prepare + 4. Review — 벤더 병렬(RunnerPool+gather), 실패 격리
    prompt = _build_prompt(pr, diff, context_text)
    with deps.worktree(Path(deps.repo_local_path), pr["head_sha"], pr["number"]) as wt:
        with tempfile.TemporaryDirectory(prefix="almighty-rt-") as rt:
            hp.prepare_runtime(runtime_dir=rt)  # ★개정: 인증 주입(전역 미상속 유지)
            vr_ids = {
                ad.vendor: review_repo.add_vendor_result(
                    conn, run_id=run_id, vendor=ad.vendor, status="running"
                )
                for ad in adapters
            }

            async def _run_one(ad):
                async def job():
                    return await ad.review(
                        prompt=prompt, workdir=Path(str(wt)), harness=hp, runtime_dir=rt
                    )

                try:
                    fs = await pool.run(job)
                    return ad.vendor, fs, None
                except Exception as e:  # 한 벤더 실패가 다른 벤더를 막지 않음
                    return ad.vendor, [], str(e)

            results = await asyncio.gather(*(_run_one(a) for a in adapters))

    all_findings = []  # (vendor_result_id, finding)
    succeeded, errors = 0, []
    for vendor, fs, err in results:
        vr_id = vr_ids[vendor]
        if err is not None:
            errors.append(f"{vendor}: {err}")
            conn.execute(
                "UPDATE vendor_result SET status='failed', error=? WHERE id=?",
                (err, vr_id),
            )
        else:
            succeeded += 1
            conn.execute("UPDATE vendor_result SET status='done' WHERE id=?", (vr_id,))
            for f in fs:
                f.vendor_result_id = vr_id  # ★개정: id() 매핑 제거, 명시 부착
                all_findings.append((vr_id, f))
    conn.commit()

    # ★개정 (codex v4 [HIGH]): enabled 벤더가 **전원 실패**면 run을 done으로
    # 오판하지 않는다. 예외로 승격 → review_pr가 run을 failed로 마감하고
    # PipelineError로 감싸 worker가 rate/timeout 시 retry한다.
    if succeeded == 0:
        raise RuntimeError("all vendors failed → " + "; ".join(errors))

    # 5. (옵션) Merge — vendor_result_id를 MergedFinding 위임으로 복원
    if repo["merge_enabled"]:
        merged = deterministic_merge([f for _, f in all_findings])
        _persist(
            conn, run_id, [(getattr(m, "vendor_result_id", None), m) for m in merged]
        )
    else:
        _persist(conn, run_id, all_findings)

    # 6. Persist done
    # ★정책 (codex v5·v6 [MEDIUM]): **부분 성공 = done**(≥1 벤더 성공). 실패 벤더는
    # vendor_result.status='failed'로 남고 **v1은 개별 벤더 자동 재시도 없음**
    # (전원 실패만 재시도). 노출 경로 = `/api/runs/{id}/vendor-results` +
    # ReviewSection 실패 배지(Task 6.2). 사용자는 이를 보고 수동 재리뷰로
    # 재실행(양 벤더 재실행). 벤더별 follow-up 자동 재시도는 v-next.
    review_repo.finish_run(conn, run_id, "done")
    pr_repo.mark_reviewed(conn, pr["id"], pr["head_sha"])


def _enabled_adapters(adapters, repo):
    out = []
    for ad in adapters:
        if ad.vendor == "claude" and not repo["vendor_claude_on"]:
            continue
        if ad.vendor == "codex" and not repo["vendor_codex_on"]:
            continue
        out.append(ad)
    return out


def _persist(conn, run_id, items):
    for vr_id, f in items:
        finding_repo.add(
            conn,
            run_id=run_id,
            vendor_result_id=vr_id,
            vendor=f.vendor,
            file=f.file,
            line=f.line,
            severity=f.severity,
            category=f.category,
            claim=f.claim,
            rationale=f.rationale,
            confidence=f.confidence,
            consensus=getattr(f, "consensus", "single"),
            consensus_group_id=getattr(f, "consensus_group_id", None),
        )


def _build_prompt(pr, diff, context_text: str) -> str:
    ctx_block = f"\n\n## 외부 컨텍스트\n{context_text}" if context_text else ""
    return (
        f"# PR #{pr['number']}: {pr['title']}\n작성자: {pr['author']}\n"
        f"{ctx_block}\n\n## Diff\n```diff\n{diff}\n```\n"
        "필요하면 레포를 읽어 맥락을 확인하라(수정 금지)."
    )
