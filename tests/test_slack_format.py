from server.slack.format import build_slack_message


def _f(severity, claim, edited_text=None):
    return {"severity": severity, "claim": claim, "edited_text": edited_text}


def test_message_structure_and_severity_order():
    msg = build_slack_message(
        full_name="acme/api",
        number=5,
        url="https://x/pull/5",
        findings=[_f("low", "minor"), _f("critical", "boom")],
    )
    lines = msg.splitlines()
    assert lines[0].startswith("🤖")
    assert "<https://x/pull/5|PR 보기>" in lines[1]
    assert "게시된 지적 2건" in lines[1]
    # critical이 low보다 먼저(severity 정렬)
    assert lines.index("• [critical] boom") < lines.index("• [low] minor")
    assert "👍 유용" in msg and "👎 불필요" in msg


def test_claim_is_mrkdwn_escaped():
    msg = build_slack_message(
        full_name="acme/api",
        number=1,
        url="",
        findings=[_f("high", "renders <script> & compares a < b")],
    )
    assert "&lt;script&gt;" in msg and "&amp;" in msg
    # 원본 제어문자가 이스케이프 없이 남지 않는다
    assert "<script>" not in msg


def test_edited_text_preferred_and_truncated():
    long = "x" * 500
    msg = build_slack_message(
        full_name="a/b",
        number=1,
        url="",
        findings=[_f("low", "orig", edited_text=long)],
    )
    line = next(ln for ln in msg.splitlines() if ln.startswith("• "))
    assert "orig" not in line  # edited_text 우선
    assert len(line) < 200  # _MAX_CLAIM_CHARS로 절단


def test_findings_capped_with_overflow_note():
    findings = [_f("low", f"c{i}") for i in range(8)]
    msg = build_slack_message(full_name="a/b", number=1, url="", findings=findings)
    bullets = [ln for ln in msg.splitlines() if ln.startswith("• ")]
    assert len(bullets) == 5
    assert "외 3건" in msg


def test_no_url_omits_link():
    msg = build_slack_message(
        full_name="a/b", number=1, url="", findings=[_f("low", "c")]
    )
    assert "PR 보기" not in msg and "게시된 지적 1건" in msg
