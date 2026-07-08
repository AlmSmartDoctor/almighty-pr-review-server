import pytest

from server.review.findings_schema import parse_findings, SchemaError


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


def test_parse_rejects_bad_severity():
    bad = (
        '```json\n{"findings":[{"file":"a","line":1,"severity":"WAT",'
        '"category":"bug","claim":"c","rationale":"r","confidence":0.5}]}\n```'
    )
    with pytest.raises(SchemaError):
        parse_findings(bad, vendor="codex")


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
