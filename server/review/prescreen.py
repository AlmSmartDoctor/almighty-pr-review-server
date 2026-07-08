import json
import re
import subprocess
from dataclasses import dataclass

_ORDER = {"trivial": 0, "moderate": 1, "complex": 2}
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class PreScreenResult:
    complexity: str  # trivial|moderate|complex
    score: float
    reason: str

    def decide(self, *, threshold: str) -> str:
        """threshold 미만 복잡도면 skip, 이상이면 review."""
        return "review" if _ORDER[self.complexity] >= _ORDER[threshold] else "skip"


PRESCREEN_TIMEOUT_SEC = 120  # ★개정: 사전평가도 subprocess → 상한 필수


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
    *, diff: str, model: str, runner=_default_runner, env=None
) -> PreScreenResult:
    """env를 넘기면 격리 config dir로 실행(build_deps가 하네스 env 주입)."""
    out = runner(["claude", "-p", PROMPT + diff, "--model", model], env=env)
    m = _FENCE.findall(out)
    if not m:
        return PreScreenResult("moderate", 0.5, "사전평가 파싱 실패→기본 리뷰")
    d = json.loads(m[-1])
    return PreScreenResult(
        complexity=d.get("complexity", "moderate"),
        score=float(d.get("score", 0.5)),
        reason=str(d.get("reason", "")),
    )
