import json
import re

from server.models import Finding

SEVERITIES = {"critical", "high", "medium", "low"}
CATEGORIES = {"bug", "security", "perf", "style", "other"}

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class SchemaError(ValueError):
    pass


def parse_findings(raw: str, *, vendor: str) -> list[Finding]:
    """CLI stdout에서 마지막 ```json 블록의 findings 배열을 추출·검증."""
    matches = _FENCE.findall(raw)
    if not matches:
        raise SchemaError("응답에 JSON 블록이 없음")
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError as e:
        raise SchemaError(f"JSON 파싱 실패: {e}") from e
    items = data.get("findings")
    if not isinstance(items, list):
        raise SchemaError("findings 배열 없음")
    out: list[Finding] = []
    for it in items:
        sev, cat = it.get("severity"), it.get("category")
        if sev not in SEVERITIES:
            raise SchemaError(f"잘못된 severity: {sev}")
        if cat not in CATEGORIES:
            raise SchemaError(f"잘못된 category: {cat}")
        out.append(
            Finding(
                vendor=vendor,
                file=str(it["file"]),
                line=int(it.get("line", 0)),
                severity=sev,
                category=cat,
                claim=str(it["claim"]),
                rationale=str(it.get("rationale", "")),
                confidence=float(it.get("confidence", 0.5)),
            )
        )
    return out


PROMPT_SCHEMA_HINT = (
    "반드시 마지막에 ```json 블록으로 다음 형식만 출력:\n"
    '{"findings":[{"file","line","severity"(critical|high|medium|low),'
    '"category"(bug|security|perf|style|other),"claim","rationale",'
    '"confidence"(0~1)}]}. 이슈 없으면 빈 배열.'
)
