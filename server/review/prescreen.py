import subprocess
from dataclasses import dataclass

from server.review.json_block import last_json_block

_ORDER = {"trivial": 0, "moderate": 1, "complex": 2}
THRESHOLDS = tuple(_ORDER)  # 설정 API 검증용 — 밖의 값은 decide()에서 KeyError

# CLI 출력이 파싱 불가일 때의 보수적 기본값. 비결정적 실패라 캐시 재사용 금지
# (다음 런이 CLI를 재시도해 self-heal 하도록) — pipeline이 이 사유로 식별.
PRESCREEN_FALLBACK_REASON = "사전평가 파싱 실패→기본 리뷰"
# CLI 실행 자체가 실패(subprocess 에러·timeout)했을 때 — 사전평가는 최적화 게이트라
# 인프라 실패가 리뷰를 막으면 안 된다. 마찬가지로 캐시 재사용 금지.
PRESCREEN_CLI_FAILURE_REASON = "사전평가 CLI 실패→기본 리뷰"


def is_nondeterministic_reason(reason: str) -> bool:
    """캐시에 등록하면 안 되는 실패 사유(다음 런이 CLI를 재시도해 self-heal)."""
    return reason == PRESCREEN_FALLBACK_REASON or reason.startswith(
        PRESCREEN_CLI_FAILURE_REASON
    )


@dataclass
class PreScreenResult:
    complexity: str  # trivial|moderate|complex
    score: float
    reason: str

    def decide(self, *, threshold: str) -> str:
        """threshold 미만 복잡도면 skip, 이상이면 review."""
        return "review" if _ORDER[self.complexity] >= _ORDER[threshold] else "skip"


PRESCREEN_TIMEOUT_SEC = 120  # ★개정: 사전평가도 subprocess → 상한 필수
MAX_INLINE_DIFF_CHARS = 100_000


def _default_runner(args, env=None, cwd=None) -> str:
    # ★개정: 벤더 계약과 동일하게 stdin 닫기 + timeout + (격리)env 적용.
    return subprocess.run(
        args,
        env=env,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=PRESCREEN_TIMEOUT_SEC,
    ).stdout


PROMPT = (
    "다음 PR diff의 리뷰 필요성을 평가하라. 코드는 읽지 말고 diff만 근거로.\n"
    '마지막에 ```json {"complexity":trivial|moderate|complex,'
    '"score":0~1,"reason":"한줄"}``` 만 출력.\n\n'
)


def prescreen(
    *, diff: str, model: str, runner=_default_runner, env=None, cwd=None
) -> PreScreenResult:
    """env/cwd를 넘기면 격리 runtime dir로 실행(build_deps가 하네스 env+격리 cwd 주입).
    프롬프트에 diff가 인라인이라 파일 접근 불필요 → cwd를 빈 runtime dir로 가둔다."""
    if len(diff) > MAX_INLINE_DIFF_CHARS:
        return PreScreenResult("complex", 1.0, diff_too_large_reason(diff))
    out = runner(["claude", "-p", PROMPT + diff, "--model", model], env=env, cwd=cwd)
    try:
        d = last_json_block(out)
        complexity = d.get("complexity", "moderate")
        if complexity in _ORDER:
            return PreScreenResult(
                complexity, float(d.get("score", 0.5)), str(d.get("reason", ""))
            )
    except (ValueError, TypeError):
        pass
    return PreScreenResult("moderate", 0.5, PRESCREEN_FALLBACK_REASON)


def diff_too_large_reason(diff: str) -> str:
    return (
        f"diff too large for inline review: {len(diff)} chars > {MAX_INLINE_DIFF_CHARS}"
    )
