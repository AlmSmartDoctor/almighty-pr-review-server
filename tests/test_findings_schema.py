import pytest

from server.review.findings_schema import PROMPT_SCHEMA_HINT, SchemaError, parse_findings


VALID = """
관련없는 서두 텍스트.
```json
{"findings": [
  {"file": "a.py", "line": 3, "severity": "high", "category": "bug",
   "claim": "널 역참조", "rationale": "x가 None일 수 있음", "confidence": 0.8}
]}
```
"""


def test_parse_extracts_fenced_json():
    fs = parse_findings(VALID, vendor="claude")
    assert len(fs) == 1
    assert fs[0].vendor == "claude"
    assert fs[0].severity == "high"


def test_parse_coerces_bad_severity_and_category():
    """모르는 severity·category는 폐기가 아니라 안전값으로 coerce(관대 파싱)."""
    raw = (
        '```json\n{"findings":[{"file":"a","line":1,"severity":"WAT",'
        '"category":"nonsense","claim":"c","rationale":"r","confidence":0.5}]}\n```'
    )
    fs = parse_findings(raw, vendor="codex")
    assert len(fs) == 1
    assert fs[0].severity == "medium"
    assert fs[0].category == "other"


def test_parse_keeps_good_findings_when_one_is_malformed():
    """한 항목의 오류가 나머지 유효 finding을 통째로 폐기하지 않는다(#1 회귀 가드)."""
    raw = (
        '```json\n{"findings":['
        '{"file":"a.py","line":"NaN","severity":"high","category":"bug",'
        '"claim":"c1","rationale":"r1","confidence":"bad"},'
        '{"file":"b.py","line":2,"severity":"low","category":"style",'
        '"claim":"c2","rationale":"r2","confidence":0.3}]}\n```'
    )
    fs = parse_findings(raw, vendor="claude")
    assert [f.file for f in fs] == ["a.py", "b.py"]
    assert fs[0].line == 0  # "NaN" → 기본 0으로 coerce
    assert fs[0].confidence == 0.5  # "bad" → 기본 0.5로 coerce


def test_parse_coerces_non_finite_line_and_confidence():
    """json은 Infinity/1e999를 float('inf')로 파싱 → int(inf)는 OverflowError.
    이게 벤더 findings 블록 전체를 폐기하지 않고 기본값으로 coerce되어야 한다(#1 회귀 가드)."""
    raw = (
        '```json\n{"findings":[{"file":"a.py","line":1e999,"severity":"high",'
        '"category":"bug","claim":"c","rationale":"r","confidence":Infinity}]}\n```'
    )
    fs = parse_findings(raw, vendor="claude")
    assert len(fs) == 1
    assert fs[0].line == 0
    assert fs[0].confidence == 0.5


def test_parse_no_json_raises():
    with pytest.raises(SchemaError):
        parse_findings("자유서술뿐, JSON 없음", vendor="claude")


def test_parse_multiple_findings_not_truncated():
    raw = (
        '```json\n{"findings":['
        '{"file":"a.py","line":1,"severity":"high","category":"bug",'
        '"claim":"c1","rationale":"r1","confidence":0.7},'
        '{"file":"b.py","line":2,"severity":"low","category":"style",'
        '"claim":"c2","rationale":"r2","confidence":0.3}]}\n```'
    )
    fs = parse_findings(raw, vendor="claude")
    assert len(fs) == 2
    assert [f.file for f in fs] == ["a.py", "b.py"]


def test_prompt_schema_requires_an_exact_added_line_location():
    assert "허용되는 실제 추가 라인 목록과 정확히 일치" in PROMPT_SCHEMA_HINT
    assert "목록 밖 위치" in PROMPT_SCHEMA_HINT


def test_parse_empty_findings_returns_empty():
    raw = '```json\n{"findings": []}\n```'
    assert parse_findings(raw, vendor="codex") == []


def test_parse_rejects_finding_missing_required_key():
    raw = (
        '```json\n{"findings":[{"line":1,"severity":"high",'
        '"category":"bug","rationale":"r","confidence":0.5}]}\n```'
    )  # no file/claim
    with pytest.raises(SchemaError):
        parse_findings(raw, vendor="claude")
