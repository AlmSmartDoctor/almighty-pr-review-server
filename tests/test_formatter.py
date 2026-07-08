from server.models import Finding
from server.formatter import build_comment, MARKER


def test_comment_has_marker_summary_and_parse_block():
    fs = [
        Finding(
            "claude", "a.py", 12, "high", "bug", "널 역참조", "x가 None일 수 있음", 0.8
        ),
        Finding("claude", "b.py", 3, "low", "style", "네이밍", "사소", 0.3),
    ]
    body = build_comment(vendor="claude", findings=fs)
    assert MARKER.format(vendor="claude") in body
    assert "high" in body and "a.py:12" in body
    # 말미 파싱용 구조 블록(학습루프 소비)
    assert "```json" in body
    assert "널 역참조" in body


def test_empty_findings_says_clean():
    body = build_comment(vendor="codex", findings=[])
    assert "발견된 이슈 없음" in body
