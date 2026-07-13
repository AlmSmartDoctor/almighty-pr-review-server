import pytest

from server.review.verify import (
    VerdictError,
    build_verify_prompt,
    parse_verdict,
)


def test_parse_verdict_refuted():
    v = parse_verdict('설명\n```json\n{"refuted":true,"rationale":"근거 없음"}\n```')
    assert v.refuted is True
    assert v.rationale == "근거 없음"


def test_parse_verdict_confirmed_defaults_rationale():
    v = parse_verdict('```json\n{"refuted":false}\n```')
    assert v.refuted is False
    assert v.rationale == ""


def test_parse_verdict_uses_last_block():
    raw = (
        '```json\n{"refuted":false}\n```\n중간\n'
        '```json\n{"refuted":true,"rationale":"최종"}\n```'
    )
    v = parse_verdict(raw)
    assert v.refuted is True and v.rationale == "최종"


def test_parse_verdict_no_block_raises():
    with pytest.raises(VerdictError):
        parse_verdict("아무 JSON 블록 없음")


def test_parse_verdict_missing_field_raises():
    with pytest.raises(VerdictError):
        parse_verdict('```json\n{"verdict":"maybe"}\n```')


def test_build_verify_prompt_contains_claim_and_diff():
    class F:
        file = "a.py"
        line = 3
        severity = "high"
        category = "bug"
        claim = "널 역참조"
        rationale = "x가 None일 수 있음"

    p = build_verify_prompt(F(), "some diff")
    assert "a.py:3" in p
    assert "널 역참조" in p
    assert "some diff" in p
    assert "수정 금지" in p
