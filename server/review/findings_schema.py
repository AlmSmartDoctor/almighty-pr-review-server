import json
import re

from server.models import Finding

SEVERITIES = {"critical", "high", "medium", "low"}
CATEGORIES = {"bug", "security", "perf", "style", "other"}

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class SchemaError(ValueError):
    pass


def parse_findings(raw: str, *, vendor: str) -> list[Finding]:
    """CLI stdout에서 마지막 ```json 블록의 findings 배열을 추출·검증.

    ★관대 파싱(prescreen.py 규율 미러): 한 항목의 사소한 오류(모르는 category·severity,
    나쁜 line/confidence)가 나머지 유효 finding을 통째로 폐기하지 않게 항목별로 coerce/skip한다.
    file·claim이 없는 항목만 버린다. 구조적 실패(펜스 없음/JSON 깨짐/findings 비배열)만 raise해
    벤더 실패로 노출한다. 빈 배열은 정상(이슈 없음)이지만, 항목이 있었는데 전부 버려지면 raise
    (파싱 불능을 조용한 초록불로 오판하지 않음)."""
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
    dropped = 0
    for it in items:
        if not isinstance(it, dict) or "file" not in it or "claim" not in it:
            dropped += 1  # file·claim 없으면 finding으로 렌더 불가 → 버림
            continue
        sev = it.get("severity") if it.get("severity") in SEVERITIES else "medium"
        cat = it.get("category") if it.get("category") in CATEGORIES else "other"
        try:  # OverflowError: json이 Infinity/1e999를 파싱 → int(inf)/float(huge int)
            line = int(it.get("line", 0))
        except (TypeError, ValueError, OverflowError):
            line = 0
        try:
            confidence = float(it.get("confidence", 0.5))
        except (TypeError, ValueError, OverflowError):
            confidence = 0.5
        if not 0.0 <= confidence <= 1.0:  # inf/nan/범위밖 → 하위 정렬·연산 오염 방지
            confidence = 0.5
        out.append(
            Finding(
                vendor=vendor,
                file=str(it["file"]),
                line=line,
                severity=sev,
                category=cat,
                claim=str(it["claim"]),
                rationale=str(it.get("rationale", "")),
                confidence=confidence,
            )
        )
    if items and not out:  # 항목이 있었는데 전부 버려짐 = 파싱 불능 → 조용한 통과 금지
        raise SchemaError(f"finding {len(items)}건 전부 형식 오류(file/claim 누락)")
    if dropped:
        print(
            f"[findings] {vendor}: 형식 오류 finding {dropped}건 스킵({len(out)}건 유지)"
        )
    return out


PROMPT_SCHEMA_HINT = (
    "반드시 마지막에 ```json 블록으로 다음 형식만 출력:\n"
    '{"findings":[{"file","line","severity"(critical|high|medium|low),'
    '"category"(bug|security|perf|style|other),"claim","rationale",'
    '"confidence"(0~1)}]}. 이슈 없으면 빈 배열.'
)
