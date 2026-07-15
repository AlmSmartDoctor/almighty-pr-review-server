import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

VERIFY_SCHEMA_HINT = (
    "위 지적이 실제 결함인지 회의적으로 재검증하라. 근거가 약하거나 오탐이면 refuted=true. "
    '반드시 마지막에 ```json 블록으로 {"refuted":true|false,"rationale":"판단 근거"} 만 출력.'
)

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class VerdictError(ValueError):
    pass


@dataclass
class Verdict:
    refuted: bool
    rationale: str = ""


@dataclass
class VerifyContext:
    diff: str
    repo_local_path: str
    head_sha: str
    pr_number: int
    harness: object
    repo_full_name: str = ""  # local_path 없을 때 온디맨드 clone 대상


def parse_verdict(raw: str) -> Verdict:
    """CLI stdout에서 마지막 ```json 블록의 verdict를 추출·검증."""
    matches = _FENCE.findall(raw)
    if not matches:
        raise VerdictError("응답에 JSON 블록이 없음")
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError as e:
        raise VerdictError(f"JSON 파싱 실패: {e}") from e
    if not isinstance(data, dict) or "refuted" not in data:
        raise VerdictError("refuted 필드 없음")
    return Verdict(
        refuted=bool(data["refuted"]),
        rationale=str(data.get("rationale", "")),
    )


def build_verify_prompt(finding, diff: str) -> str:
    return (
        "# 리뷰 지적 재검증\n"
        f"- 파일: {finding.file}:{finding.line}\n"
        f"- 심각도/분류: {finding.severity}/{finding.category}\n"
        f"- 주장: {finding.claim}\n"
        f"- 근거: {finding.rationale}\n\n"
        f"## Diff\n```diff\n{diff}\n```\n"
        "필요하면 레포를 읽어 확인하라(수정 금지)."
    )


def _pick_verifier(by_vendor: dict, author_vendor: str):
    """저자가 자기 지적을 변호하지 않도록 다른 벤더를 우선 검증자로 쓴다."""
    for name, ad in by_vendor.items():
        if name != author_vendor:
            return ad
    return by_vendor.get(author_vendor)


def make_verifier(adapters, worktree, clone=None):
    """gh_deps 배선용 실 검증기. 리뷰 블록과 독립된 자체 worktree/runtime을 열어
    고위험 SINGLE finding을 다른 벤더로 반박 검증한다. 실패는 confirmed로 degrade
    (검증이 리뷰 결과를 절대 삭제하지 않는다). local_path 없으면 clone으로 온디맨드."""
    from server.review.worktree import checkout

    async def verify(targets, ctx: VerifyContext):
        by_vendor = {a.vendor: a for a in adapters}
        verdicts = []
        with checkout(
            worktree,
            clone,
            local_path=ctx.repo_local_path,
            full_name=ctx.repo_full_name,
            sha=ctx.head_sha,
            pr_number=ctx.pr_number,
        ) as wt:
            with tempfile.TemporaryDirectory(prefix="almighty-vf-") as rt:
                ctx.harness.prepare_runtime(runtime_dir=rt)
                for m in targets:
                    ad = _pick_verifier(by_vendor, m.vendor)
                    if ad is None:
                        verdicts.append(Verdict(refuted=False))
                        continue
                    try:
                        v = await ad.verify(
                            prompt=build_verify_prompt(m, ctx.diff),
                            workdir=Path(str(wt)),
                            harness=ctx.harness,
                            runtime_dir=rt,
                        )
                    except Exception:
                        v = Verdict(refuted=False)  # degrade: 확정 취급(삭제 아님)
                    verdicts.append(v)
        return verdicts

    return verify
