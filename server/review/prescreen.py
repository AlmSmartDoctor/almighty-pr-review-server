import subprocess
from dataclasses import dataclass

from server import config
from server.review.json_block import last_json_block
from server.review.vendors import run_bounded_process_sync

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
    return PRESCREEN_FALLBACK_REASON in reason or PRESCREEN_CLI_FAILURE_REASON in reason


@dataclass
class PreScreenResult:
    complexity: str  # trivial|moderate|complex
    score: float
    reason: str

    def decide(self, *, threshold: str) -> str:
        """threshold 미만 복잡도면 skip, 이상이면 review."""
        return "review" if _ORDER[self.complexity] >= _ORDER[threshold] else "skip"


PRESCREEN_TIMEOUT_SEC = 120  # ★개정: 사전평가도 subprocess → 상한 필수
PRESCREEN_STREAM_LIMIT_BYTES = 256 * 1024
MAX_INLINE_DIFF_CHARS = 100_000


def is_valid_prescreen_model(model: str | None) -> bool:
    """Claude CLI가 해석할 수 있는 제안 별칭 또는 정식 Claude 모델 ID만 허용한다."""
    value = (model or "").strip()
    lowered = value.lower()
    return bool(value) and (
        value in config.CLAUDE_MODELS
        or (
            lowered.startswith("claude-")
            and any(character.isdigit() for character in lowered[len("claude-") :])
        )
    )


def normalize_prescreen_model(model: str | None) -> tuple[str, str | None]:
    """사전평가는 Claude CLI 전용이다. 비어 있거나 타 벤더/임의 모델이면 안전한
    기본값으로 폴백하고, 호출자가 감사 메타데이터에 남길 사유를 함께 반환한다.
    미래 Claude 정식 ID는 claude- 접두사로 허용한다."""
    value = (model or "").strip()
    if not value:
        return config.DEFAULT_PRESCREEN_MODEL, "empty_model_fallback"
    if not is_valid_prescreen_model(value):
        return config.DEFAULT_PRESCREEN_MODEL, "non_claude_model_fallback"
    return value, None


def _default_runner(args, env=None, cwd=None, input_text=None) -> str:
    result = run_bounded_process_sync(
        args,
        env=env,
        cwd=cwd,
        timeout=PRESCREEN_TIMEOUT_SEC,
        stream_limit=PRESCREEN_STREAM_LIMIT_BYTES,
        input_text=input_text,
    )
    if result.stdout_truncated or result.stderr_truncated:
        raise RuntimeError("prescreen output limit exceeded")
    if result.exit_code != 0:
        raise subprocess.CalledProcessError(result.exit_code, args)
    return result.stdout


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
    out = runner(
        [
            "claude", "-p", "--permission-mode", "plan",
            "--tools", "", "--disable-slash-commands", "--model", model,
        ],
        env=env,
        cwd=cwd,
        input_text=PROMPT + diff,
    )
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
