_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_MAX_LINES = 5  # Slack 메시지에 나열할 finding 상한(나머지는 생략 표기)
_MAX_CLAIM_CHARS = 140


def _one_line(text: str) -> str:
    return " ".join((text or "").split())[:_MAX_CLAIM_CHARS]


def _esc(text: str) -> str:
    """Slack mrkdwn 제어문자 이스케이프(& 먼저). finding claim은 LLM/사람 생성이라
    'List<T>'·'a < b'·'<url|text>' 같은 값이 메시지를 깨거나 링크를 주입할 수 있다."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_slack_message(*, full_name: str, number: int, url: str, findings) -> str:
    """게시된 리뷰를 Slack mrkdwn으로 요약한다. finding은 sqlite3.Row(또는 dict) 시퀀스로
    severity/category/claim/edited_text를 갖는다. 팀이 👍/👎로 평가하도록 유도하는 한 줄을
    끝에 붙인다(이 메시지의 반응이 학습 신호가 된다). ##/HTML 마커가 없는 순수 mrkdwn."""
    ordered = sorted(findings, key=lambda f: _SEV_ORDER.get(f["severity"], 9))
    head = f"🤖 *almighty 리뷰* — {full_name} #{number}"
    link = (
        f"<{url}|PR 보기> · 게시된 지적 {len(ordered)}건"
        if url
        else f"게시된 지적 {len(ordered)}건"
    )
    lines = [head, link]
    for i, f in enumerate(ordered):
        if i >= _MAX_LINES:
            lines.append(f"_…외 {len(ordered) - _MAX_LINES}건_")
            break
        claim = _esc(_one_line(f["edited_text"] or f["claim"]))
        lines.append(f"• [{f['severity']}] {claim}")
    lines.append("_이 리뷰가 유용했나요? 👍 유용 · 👎 불필요 로 평가해 주세요._")
    return "\n".join(lines)
