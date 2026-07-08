import json

from server.models import Finding

MARKER = "<!-- almighty-review [{vendor}] -->"
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def build_comment(*, vendor: str, findings: list[Finding]) -> str:
    marker = MARKER.format(vendor=vendor)
    if not findings:
        return f"{marker}\n## 🤖 {vendor} 리뷰\n\n발견된 이슈 없음. ✅\n"
    ordered = sorted(findings, key=lambda f: _SEV_ORDER.get(f.severity, 9))
    top = ordered[0].severity
    lines = [
        marker,
        f"## 🤖 {vendor} 리뷰",
        f"> 요약: **{len(findings)}건** · 최고 severity **{top}**\n",
    ]
    for f in ordered:
        lines.append(
            f"### [{f.severity}] `{f.file}:{f.line}` — {f.category}\n"
            f"- **주장:** {f.claim}\n"
            f"- **근거:** {f.rationale}\n"
            f"- **확신도:** {f.confidence:.2f}\n"
        )
    parse_block = {
        "vendor": vendor,
        "findings": [
            {
                "file": f.file,
                "line": f.line,
                "severity": f.severity,
                "category": f.category,
                "claim": f.claim,
                "rationale": f.rationale,
                "confidence": f.confidence,
            }
            for f in ordered
        ],
    }
    lines.append(
        "\n<details><summary>machine-readable</summary>\n\n"
        "```json\n"
        + json.dumps(parse_block, ensure_ascii=False, indent=2)
        + "\n```\n</details>"
    )
    return "\n".join(lines)
