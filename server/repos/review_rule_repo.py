"""Repository-scoped review rules promoted from repeated human feedback."""

_MIN_CATEGORY_DECISIONS = 3
_MIN_REJECTED = 3
_MIN_REJECTION_RATIO = 2 / 3
_ALLOWED_STATUSES = {"active", "disabled"}


def _row(row) -> dict:
    return {key: row[key] for key in row.keys()}


def list_for_repo(conn, repo_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, repo_id, category, text, status,
                  evidence_total, evidence_rejected, created_at, updated_at
           FROM review_rule
           WHERE repo_id=?
           ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'proposed' THEN 1 ELSE 2 END,
                    category COLLATE NOCASE, id""",
        (repo_id,),
    ).fetchall()
    return [_row(row) for row in rows]


def active_for_repo_name(conn, full_name: str, *, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """SELECT rr.id, rr.repo_id, rr.category, rr.text, rr.status,
                  rr.evidence_total, rr.evidence_rejected,
                  rr.created_at, rr.updated_at
           FROM review_rule rr
           JOIN repo r ON r.id = rr.repo_id
           WHERE r.full_name=? COLLATE NOCASE AND rr.status='active'
           ORDER BY rr.category COLLATE NOCASE, rr.id
           LIMIT ?""",
        (full_name, limit),
    ).fetchall()
    return [_row(row) for row in rows]


def _proposal_text(category: str) -> str:
    return (
        f"{category} 범주의 지적은 명확한 동작 영향이나 유지보수 위험이 있을 때만 제기하고, "
        "취향·스타일 차이만으로는 지적하지 않는다."
    )


def propose_rules(conn, repo_id: int, categories) -> list[dict]:
    """기각이 충분히 반복된 카테고리만 제안한다. 기존 상태는 절대 자동 변경하지 않는다."""
    for stat in categories:
        category = " ".join(str(stat.get("category") or "").split())[:64]
        approved = max(0, int(stat.get("approved") or 0))
        edited = max(0, int(stat.get("edited") or 0))
        rejected = max(0, int(stat.get("rejected") or 0))
        total = approved + edited + rejected
        if (
            not category
            or total < _MIN_CATEGORY_DECISIONS
            or rejected < _MIN_REJECTED
            or rejected / total < _MIN_REJECTION_RATIO
        ):
            continue
        conn.execute(
            """INSERT INTO review_rule
                 (repo_id, category, text, status, evidence_total, evidence_rejected,
                  created_at, updated_at)
               VALUES (?, ?, ?, 'proposed', ?, ?, datetime('now'), datetime('now'))
               ON CONFLICT(repo_id, category) DO UPDATE SET
                 text=excluded.text,
                 evidence_total=excluded.evidence_total,
                 evidence_rejected=excluded.evidence_rejected,
                 updated_at=datetime('now')""",
            (repo_id, category, _proposal_text(category), total, rejected),
        )
    conn.commit()
    return list_for_repo(conn, repo_id)


def set_status(conn, rule_id: int, status: str):
    if status not in _ALLOWED_STATUSES:
        raise ValueError("status must be active or disabled")
    cur = conn.execute(
        """UPDATE review_rule
           SET status=?, updated_at=datetime('now')
           WHERE id=?""",
        (status, rule_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    row = conn.execute(
        """SELECT id, repo_id, category, text, status,
                  evidence_total, evidence_rejected, created_at, updated_at
           FROM review_rule WHERE id=?""",
        (rule_id,),
    ).fetchone()
    return _row(row)
