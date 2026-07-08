from server.review.prescreen import prescreen, PreScreenResult


FAKE = (
    '판단\n```json\n{"complexity":"moderate","score":0.5,'
    '"reason":"핵심 로직 변경"}\n```'
)


def test_prescreen_parses(tmp_path):
    def runner(args, env=None, cwd=None):
        assert "--model" in args  # 가벼운 모델 지정
        return FAKE

    res = prescreen(diff="diff...", model="haiku", runner=runner)
    assert isinstance(res, PreScreenResult)
    assert res.complexity == "moderate"
    assert res.reason


def test_prescreen_gate_decision():
    res = PreScreenResult(complexity="trivial", score=0.1, reason="오타")
    assert res.decide(threshold="moderate") == "skip"
    res2 = PreScreenResult(complexity="complex", score=0.9, reason="x")
    assert res2.decide(threshold="moderate") == "review"
