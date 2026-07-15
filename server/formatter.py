from server import config
from server.models import Finding

MARKER = "<!-- almighty-review [{vendor}] -->"
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# 사내 PR 봇이 리뷰 본문을 Slack 메시지(≈3000자 한도)로 relay한다 — 초과하면 relay 자체가
# 실패해 알림이 아예 가지 않는다(실측: 3183자 미중계, 짧은 본문 즉시 중계). 여유를 두고 자르며,
# 잘린 finding의 상세는 diff 라인 인라인 코멘트(build_inline_comment)에 그대로 남는다.
_BODY_MAX_LEN = 2600


def _banner() -> str:
    b = config.POST_BANNER.strip()
    return f"{b}\n\n" if b else ""


def build_inline_comment(f: Finding) -> str:
    """PR review 인라인(라인) 코멘트 본문 — finding 하나를 diff 라인에 붙인다.
    배너는 review 최상단 본문(build_comment)에만 두고 인라인마다 반복하지 않는다.
    f.claim은 이미 edited_text-or-claim으로 해석된 값(_row_to_finding)."""
    return (
        f"{MARKER.format(vendor=f.vendor)}\n"
        f"**[{f.severity} / {f.category}]** {f.claim}\n\n"
        f"{f.rationale}\n\n"
        f"확신도 {f.confidence:.2f}"
    )


def build_comment(*, vendor: str, findings: list[Finding]) -> str:
    marker = MARKER.format(vendor=vendor)
    if not findings:
        return f"{_banner()}{marker}\n## 🤖 {vendor} 리뷰\n\n발견된 이슈 없음. ✅\n"
    ordered = sorted(findings, key=lambda f: _SEV_ORDER.get(f.severity, 9))
    top = ordered[0].severity
    body = (
        f"{_banner()}{marker}\n"
        f"## 🤖 {vendor} 리뷰\n"
        f"> 요약: **{len(findings)}건** · 최고 severity **{top}**\n"
    )
    for i, f in enumerate(ordered):
        section = (
            f"\n### [{f.severity}] `{f.file}:{f.line}` — {f.category}\n"
            f"- **주장:** {f.claim}\n"
            f"- **근거:** {f.rationale}\n"
            f"- **확신도:** {f.confidence:.2f}\n"
        )
        # 최소 1건(최고 severity)은 항상 싣고, 한도를 넘기면 낮은 severity부터 생략한다.
        if i > 0 and len(body) + len(section) > _BODY_MAX_LEN:
            body += f"\n_…severity 낮은 {len(ordered) - i}건 생략 — 상세는 인라인 코멘트 참고_\n"
            break
        body += section
    return body
