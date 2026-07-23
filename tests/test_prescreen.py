from server.review.prescreen import (
    MAX_INLINE_DIFF_CHARS,
    PreScreenResult,
    is_valid_prescreen_model,
    normalize_prescreen_model,
    prescreen,
)


FAKE = (
    '판단\n```json\n{"complexity":"moderate","score":0.5,'
    '"reason":"핵심 로직 변경"}\n```'
)


def test_prescreen_model_normalization_rejects_non_claude_models():
    assert normalize_prescreen_model("gpt-5.6-terra") == (
        "haiku",
        "non_claude_model_fallback",
    )
    assert normalize_prescreen_model("") == ("haiku", "empty_model_fallback")
    assert normalize_prescreen_model("claude-future-1") == ("claude-future-1", None)
    assert normalize_prescreen_model("gemini-2.5-pro") == (
        "haiku",
        "non_claude_model_fallback",
    )
    assert normalize_prescreen_model("claude-haiku") == (
        "haiku",
        "non_claude_model_fallback",
    )
    assert is_valid_prescreen_model("sonnet") is True
    assert is_valid_prescreen_model("gpt-5.6-sol") is False
    assert is_valid_prescreen_model("llama-3") is False
    assert is_valid_prescreen_model("claude-haiku") is False


def test_prescreen_parses(tmp_path):
    def runner(args, env=None, cwd=None, input_text=None):
        assert "--model" in args  # 가벼운 모델 지정
        assert "--permission-mode" in args
        assert args[args.index("--tools") + 1] == ""
        assert "--disable-slash-commands" in args
        assert "diff..." not in " ".join(args)
        assert "diff..." in input_text
        return FAKE

    res = prescreen(diff="diff...", model="haiku", runner=runner)
    assert isinstance(res, PreScreenResult)
    assert res.complexity == "moderate"
    assert res.reason


def test_prescreen_confines_runner_cwd():
    captured = {}

    def runner(args, env=None, cwd=None, input_text=None):
        captured["cwd"] = cwd
        return FAKE

    prescreen(diff="diff...", model="haiku", runner=runner, cwd="/tmp/isolated-rt")
    assert captured["cwd"] == "/tmp/isolated-rt"  # 격리 runtime dir로 가둠


def test_prescreen_gate_decision():
    res = PreScreenResult(complexity="trivial", score=0.1, reason="오타")
    assert res.decide(threshold="moderate") == "skip"
    res2 = PreScreenResult(complexity="complex", score=0.9, reason="x")
    assert res2.decide(threshold="moderate") == "review"


def test_prescreen_falls_back_on_no_block():
    res = prescreen(diff="x", model="m", runner=lambda *a, **k: "no json here")
    assert res.complexity == "moderate"
    assert res.decide(threshold="moderate") == "review"


def test_prescreen_falls_back_on_malformed_json():
    bad = '```json\n{"complexity":moderate,}\n```'  # unquoted value + trailing comma
    res = prescreen(diff="x", model="m", runner=lambda *a, **k: bad)
    assert res.decide(threshold="moderate") == "review"


def test_prescreen_normalizes_unknown_complexity():
    j = '```json\n{"complexity":"huge","score":0.9,"reason":"x"}\n```'
    res = prescreen(diff="x", model="m", runner=lambda *a, **k: j)
    assert res.decide(threshold="moderate") == "review"  # no KeyError at gate time


def test_prescreen_skips_llm_when_diff_too_large():
    def runner(*args, **kwargs):
        raise AssertionError("runner should not be called")

    res = prescreen(diff="x" * (MAX_INLINE_DIFF_CHARS + 1), model="m", runner=runner)

    assert res.complexity == "complex"
    assert res.score == 1.0
    assert "diff too large" in res.reason
