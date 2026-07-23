"""Render human-approved repository review rules as bounded review context."""

from server import config
from server.db import connect
from server.repos import review_rule_repo

_MAX_RULES = 20
_MAX_OUTPUT_CHARS = 4_000


def active_review_rules_source(*, db_path=None):
    path = db_path if db_path is not None else config.DB_PATH

    def source(req) -> str:
        conn = connect(path)
        try:
            rules = review_rule_repo.active_for_repo_name(
                conn, req.repo, limit=_MAX_RULES
            )
        finally:
            conn.close()
        if not rules:
            return ""
        lines = ["팀이 승인한 리뷰 규칙(이 레포에만 적용):"]
        for rule in rules:
            line = (
                f"- [{rule['category']}] {rule['text']} "
                f"(근거: 기각 {rule['evidence_rejected']}/{rule['evidence_total']}건)"
            )
            if len("\n".join(lines + [line])) > _MAX_OUTPUT_CHARS:
                break
            lines.append(line)
        return "\n".join(lines) if len(lines) > 1 else ""

    return source
