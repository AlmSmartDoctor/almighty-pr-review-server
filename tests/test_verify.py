import asyncio
from contextlib import contextmanager

import pytest

from server.review.harness import RuntimeCredentialError
from server.review.verify import (
    Verdict,
    VerdictError,
    _debate,
    build_rebuttal_prompt,
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


class _Finding:
    file = "a.py"
    line = 3
    severity = "high"
    category = "bug"
    claim = "널 역참조"
    rationale = "x가 None일 수 있음"
    vendor = "claude"


class _FakeVerifier:
    """스크립트된 Verdict(또는 raise할 Exception)를 순서대로 반환하고 호출을 기록."""

    def __init__(self, vendor, scripted):
        self.vendor = vendor
        self._scripted = list(scripted)
        self.calls = []

    async def verify(self, *, prompt, workdir, harness, runtime_dir):
        self.calls.append(prompt)
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _run_debate(refuter, author):
    return asyncio.run(
        _debate(
            _Finding(),
            refuter=refuter,
            author=author,
            diff="some diff",
            harness=None,
            workdir="wd",
            runtime_dir="rt",
        )
    )


def test_debate_confirmed_skips_author_when_no_refute():
    refuter = _FakeVerifier("codex", [Verdict(refuted=False, rationale="실제 버그")])
    author = _FakeVerifier("claude", [])  # 호출되면 IndexError → 미호출을 증명
    v = _run_debate(refuter, author)
    assert v.refuted is False and v.contested is False
    assert v.independent is True
    assert v.evidence_status == "independent_model_support"
    assert author.calls == []


def test_debate_same_vendor_support_is_not_independent():
    only = _FakeVerifier("claude", [Verdict(refuted=False, rationale="자체 확인")])
    v = _run_debate(only, only)
    assert v.refuted is False
    assert v.independent is False
    assert v.evidence_status == "supported_self"


def test_debate_refuted_when_author_concedes():
    refuter = _FakeVerifier("codex", [Verdict(refuted=True, rationale="근거 약함")])
    author = _FakeVerifier("claude", [Verdict(refuted=True, rationale="맞다 오탐")])
    v = _run_debate(refuter, author)
    assert v.refuted is True and v.contested is False
    assert len(author.calls) == 1  # 2라운드 저자 변호가 실제로 실행됨


def test_debate_contested_when_author_defends():
    refuter = _FakeVerifier("codex", [Verdict(refuted=True, rationale="오탐이다")])
    author = _FakeVerifier("claude", [Verdict(refuted=False, rationale="실제 결함")])
    v = _run_debate(refuter, author)
    assert v.refuted is False and v.contested is True
    assert "오탐이다" in v.rationale and "실제 결함" in v.rationale  # 양측 근거 보존


def test_debate_keeps_refute_when_no_distinct_author():
    refuter = _FakeVerifier("codex", [Verdict(refuted=True, rationale="근거 약함")])
    v = _run_debate(refuter, None)
    assert v.refuted is True and v.contested is False


def test_debate_marks_degraded_when_no_refuter():
    # 검증이 실행되지 않았는데 confirmed로 라벨되면 사람이 '확인됨'으로 오신뢰한다.
    v = _run_debate(None, None)
    assert v.refuted is False and v.contested is False
    assert v.degraded is True


def test_debate_marks_degraded_when_round1_raises():
    refuter = _FakeVerifier("codex", [RuntimeError("boom")])
    author = _FakeVerifier("claude", [])
    v = _run_debate(refuter, author)
    assert v.refuted is False and v.contested is False
    assert v.degraded is True
    assert author.calls == []


def test_debate_propagates_runtime_cleanup_failure():
    class Harness:
        @contextmanager
        def runtime_credentials(self, **kwargs):
            yield
            raise RuntimeCredentialError("runtime_cleanup_failed")

    with pytest.raises(RuntimeCredentialError, match="runtime_cleanup_failed"):
        asyncio.run(
            _debate(
                _Finding(),
                refuter=_FakeVerifier(
                    "codex", [Verdict(refuted=False, rationale="valid")]
                ),
                author=None,
                diff="some diff",
                harness=Harness(),
                workdir="wd",
                runtime_dir="rt",
            )
        )


def test_debate_does_not_degrade_exception_with_cleanup_failure_note():
    class Harness:
        @contextmanager
        def runtime_credentials(self, **kwargs):
            try:
                yield
            except BaseException as exc:
                exc.add_note("runtime_cleanup_failed")
                raise

    with pytest.raises(RuntimeCredentialError, match="runtime_cleanup_failed"):
        asyncio.run(
            _debate(
                _Finding(),
                refuter=_FakeVerifier("codex", [RuntimeError("verify failed")]),
                author=None,
                diff="some diff",
                harness=Harness(),
                workdir="wd",
                runtime_dir="rt",
            )
        )


def test_debate_keeps_refute_when_rebuttal_raises():
    refuter = _FakeVerifier("codex", [Verdict(refuted=True, rationale="근거 약함")])
    author = _FakeVerifier("claude", [RuntimeError("boom")])
    v = _run_debate(refuter, author)
    assert v.refuted is True and v.contested is False


def test_build_rebuttal_prompt_contains_challenge_and_claim():
    p = build_rebuttal_prompt(_Finding(), "some diff", "반박 근거 XYZ")
    assert "널 역참조" in p
    assert "반박 근거 XYZ" in p
    assert "some diff" in p
    assert "수정 금지" in p
