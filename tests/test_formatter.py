from server import config
from server.models import Finding
from server.formatter import build_comment, build_inline_comment, MARKER


def test_comment_has_marker_summary_and_findings():
    fs = [
        Finding(
            "claude", "a.py", 12, "high", "bug", "널 역참조", "x가 None일 수 있음", 0.8
        ),
        Finding("claude", "b.py", 3, "low", "style", "네이밍", "사소", 0.3),
    ]
    body = build_comment(vendor="claude", findings=fs)
    assert MARKER.format(vendor="claude") in body
    assert "high" in body and "a.py:12" in body
    assert "널 역참조" in body
    assert body.index("[high]") < body.index("[low]")  # severity 정렬
    assert "최고 severity **high**" in body  # top = 최고 severity
    assert "0.80" in body  # .2f 포맷(0.8 아님)
    # machine-readable JSON 블록은 제거됨(write-only·미사용, PR 봇 relay 길이만 부풀림)
    assert "```json" not in body
    assert "machine-readable" not in body


def test_comment_capped_below_relay_limit_drops_low_severity():
    # PR 봇 relay의 Slack 메시지 한도(≈3000자)를 넘기지 않도록 낮은 severity부터 생략한다.
    long_rationale = "근거 " * 200  # ~1000자
    fs = [
        Finding("claude", f"f{i}.py", i, sev, "bug", f"주장{i}", long_rationale, 0.5)
        for i, sev in enumerate(["critical", "high", "medium", "low", "low", "low"])
    ]
    body = build_comment(vendor="claude", findings=fs)
    assert len(body) <= 2600 + 400  # 캡 + 마지막 섹션/생략주석 여유
    assert "생략" in body  # 일부 생략 주석
    assert "critical" in body  # 최고 severity는 항상 포함
    assert "요약: **6건**" in body  # 요약 건수는 전체 기준


def test_empty_findings_says_clean():
    body = build_comment(vendor="codex", findings=[])
    assert "발견된 이슈 없음" in body


def test_banner_prepended_above_marker_when_set(monkeypatch):
    monkeypatch.setattr(config, "POST_BANNER", "[스닥 리뷰 서버 테스트 중]")
    fs = [Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)]
    body = build_comment(vendor="claude", findings=fs)
    assert body.startswith("[스닥 리뷰 서버 테스트 중]\n\n")
    assert body.index("[스닥 리뷰 서버 테스트 중]") < body.index(
        MARKER.format(vendor="claude")
    )
    # 빈 결과 경로도 배너가 붙는다
    empty = build_comment(vendor="codex", findings=[])
    assert empty.startswith("[스닥 리뷰 서버 테스트 중]\n\n")


def test_no_banner_when_unset(monkeypatch):
    monkeypatch.setattr(config, "POST_BANNER", "")
    body = build_comment(
        vendor="claude",
        findings=[Finding("claude", "a.py", 1, "high", "bug", "c", "r", 0.8)],
    )
    assert body.startswith(MARKER.format(vendor="claude"))


def test_inline_comment_has_marker_finding_detail_no_banner(monkeypatch):
    monkeypatch.setattr(config, "POST_BANNER", "[스닥 리뷰 서버 테스트 중]")
    f = Finding("codex", "a.py", 7, "high", "security", "SQL 주입", "미검증 입력", 0.82)
    body = build_inline_comment(f)
    assert MARKER.format(vendor="codex") in body
    assert "[high / security]" in body
    assert "SQL 주입" in body and "미검증 입력" in body
    assert "0.82" in body
    # 배너는 review 최상단 본문에만 — 인라인 코멘트마다 반복하지 않는다
    assert "[스닥 리뷰 서버 테스트 중]" not in body


def test_unknown_severity_sorts_last():
    fs = [
        Finding("claude", "z.py", 1, "weird", "other", "미상", "r", 0.5),
        Finding("claude", "a.py", 2, "critical", "bug", "치명", "r", 0.9),
    ]
    body = build_comment(vendor="claude", findings=fs)
    assert body.index("[critical]") < body.index("[weird]")
    assert "최고 severity **critical**" in body
