import json
import re

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def last_json_block(raw: str) -> dict:
    """CLI stdout에서 마지막 ```json 펜스 블록을 파싱해 반환.
    펜스 없음 → ValueError, 파싱 실패 → json.JSONDecodeError(ValueError 서브클래스).
    실패 정책(폴백/에러 변환)은 호출부가 정한다."""
    matches = _FENCE.findall(raw)
    if not matches:
        raise ValueError("응답에 JSON 블록이 없음")
    return json.loads(matches[-1])
