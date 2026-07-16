import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from server.review.json_block import last_json_block

VERIFY_SCHEMA_HINT = (
    "위 지적이 실제 결함인지 회의적으로 재검증하라. 근거가 약하거나 오탐이면 refuted=true. "
    '반드시 마지막에 ```json 블록으로 {"refuted":true|false,"rationale":"판단 근거"} 만 출력.'
)


class VerdictError(ValueError):
    pass


@dataclass
class Verdict:
    refuted: bool
    rationale: str = ""
    contested: bool = False  # 반박당했으나 저자가 방어 → 견해 대립(반감하지 않음)
    degraded: bool = False  # 검증이 실제로 실행되지 못함 → 라벨 부착 금지(오노출 방지)


@dataclass
class VerifyContext:
    diff: str
    repo_local_path: str
    head_sha: str
    pr_number: int
    harness: object
    repo_full_name: str = ""  # local_path 없을 때 서비스 전용 clone 대상


def parse_verdict(raw: str) -> Verdict:
    """CLI stdout에서 마지막 ```json 블록의 verdict를 추출·검증."""
    try:
        data = last_json_block(raw)
    except json.JSONDecodeError as e:
        raise VerdictError(f"JSON 파싱 실패: {e}") from e
    except ValueError as e:
        raise VerdictError(str(e)) from e
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


def build_rebuttal_prompt(finding, diff: str, challenge: str) -> str:
    """반박당한 지적의 저자 벤더에게 변호 기회를 준다. 스키마 힌트(오탐이면 refuted=true)에
    맞춰, 반박이 타당해 지적이 오탐이면 refuted=true, 반박이 틀렸고 지적이 유효하면 false."""
    return (
        "# 리뷰 지적에 대한 반박 재검토\n"
        f"- 파일: {finding.file}:{finding.line}\n"
        f"- 심각도/분류: {finding.severity}/{finding.category}\n"
        f"- 원래 주장: {finding.claim}\n"
        f"- 원래 근거: {finding.rationale}\n\n"
        f"## 다른 벤더의 반박\n{challenge}\n\n"
        f"## Diff\n```diff\n{diff}\n```\n"
        "이 반박이 타당한가? 지적이 실제로 오탐이면 refuted=true, 반박이 틀렸고 "
        "지적이 유효하면 refuted=false. 필요하면 레포를 읽어 확인하라(수정 금지)."
    )


async def _debate(finding, *, refuter, author, diff, harness, workdir, runtime_dir):
    """2라운드 디베이트: 상대 벤더가 반박(1R) → 반박되면 저자 벤더가 변호(2R).
    - 반박 안 됨 → confirmed(반대 의견 없음)
    - 반박됨 + 저자 수긍 → refuted(호출부가 confidence 반감)
    - 반박됨 + 저자 방어 → contested(반감하지 않고 사람에게 노출)
    검증/디베이트 실패는 리뷰 결과를 삭제하지 않도록 보수적으로 degrade한다(B-INV-4/8)."""

    async def ask(ad, prompt):
        return await ad.verify(
            prompt=prompt, workdir=workdir, harness=harness, runtime_dir=runtime_dir
        )

    if refuter is None:
        return Verdict(
            refuted=False, degraded=True
        )  # 검증 미실행 — confirmed 오라벨 방지
    try:
        r1 = await ask(refuter, build_verify_prompt(finding, diff))
    except Exception:
        return Verdict(refuted=False, degraded=True)  # degrade: 라벨 없이 원본 유지
    if not r1.refuted:
        return Verdict(refuted=False, rationale=r1.rationale)  # 반대 의견 없음
    if author is None or author is refuter:
        return Verdict(refuted=True, rationale=r1.rationale)  # 변호할 저자 없음
    try:
        r2 = await ask(author, build_rebuttal_prompt(finding, diff, r1.rationale))
    except Exception:
        return Verdict(refuted=True, rationale=r1.rationale)  # 변호 실패 → 반박 유지
    if r2.refuted:  # 저자도 오탐 인정 → 반박 확정
        return Verdict(refuted=True, rationale=r1.rationale)
    return Verdict(  # 저자가 방어 → 견해 대립(반감하지 않음), 양측 근거 보존
        refuted=False,
        contested=True,
        rationale=f"반박: {r1.rationale} / 저자 변호: {r2.rationale}",
    )


def _pick_verifier(by_vendor: dict, author_vendor: str):
    """저자가 자기 지적을 변호하지 않도록 다른 벤더를 우선 검증자로 쓴다."""
    for name, ad in by_vendor.items():
        if name != author_vendor:
            return ad
    return by_vendor.get(author_vendor)


def make_verifier(adapters, worktree, clone=None):
    """gh_deps 배선용 실 검증기. 리뷰 블록과 독립된 자체 worktree/runtime을 열어
    고위험 SINGLE finding을 다른 벤더로 반박 검증한다. 실패는 degraded verdict로
    (검증이 리뷰 결과를 삭제하지도, confirmed로 오라벨하지도 않는다).
    local_path 없으면 clone으로 온디맨드."""
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
                    verdicts.append(
                        await _debate(
                            m,
                            refuter=_pick_verifier(by_vendor, m.vendor),
                            author=by_vendor.get(m.vendor),
                            diff=ctx.diff,
                            harness=ctx.harness,
                            workdir=Path(str(wt)),
                            runtime_dir=rt,
                        )
                    )
        return verdicts

    return verify
