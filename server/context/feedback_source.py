from server import config
from server.db import connect

_MAX_DECISIONS = 400  # 최근 결정만 스캔(비용 상한)
_MIN_DECISIONS = 3  # 이 미만이면 신뢰할 패턴이 아님 → 미주입
_MAX_EXAMPLES = 5  # 기각/수정 대표 예시 각 버킷 상한
_MAX_CLAIM_CHARS = 160  # 예시 claim 1줄 절단

# finding엔 repo_id가 없어 review_run→pull_request→repo로 조인. review_run.status와
# 충돌하지 않게 finding 컬럼을 명시 alias. 최근순(id DESC)으로 캡 스캔.
_QUERY = """
SELECT f.category AS category, f.status AS status,
       f.claim AS claim, f.edited_text AS edited_text
FROM finding f
JOIN review_run rr ON rr.id = f.run_id
JOIN pull_request p ON p.id = rr.pr_id
JOIN repo r ON r.id = p.repo_id
WHERE r.full_name = ? COLLATE NOCASE
  AND f.status IN ('approved', 'dismissed', 'edited', 'posted')
ORDER BY f.id DESC
LIMIT ?
"""


def _one_line(claim: str) -> str:
    return " ".join((claim or "").split())[:_MAX_CLAIM_CHARS]


def _verdict(status: str, edited_text) -> str:
    if status == "dismissed":
        return "rejected"
    if status == "edited" or (edited_text and edited_text.strip()):
        return "edited"
    return "approved"


def summarize_feedback(rows) -> str:
    """사람 판단 finding 행들을 카테고리별 수용/기각 집계 + 대표 예시로 요약(순수 함수).
    결정 수가 _MIN_DECISIONS 미만이면 ""(신뢰할 패턴 아님)."""
    tally = {}  # category -> [accepted, rejected]
    rejected, edited = [], []  # 대표 예시 (category, claim), 중복 제거·최근순
    seen_rej, seen_ed = set(), set()
    total = 0
    for row in rows:
        cat = (row["category"] or "").strip() or "기타"
        claim = _one_line(row["claim"])
        verdict = _verdict(row["status"], row["edited_text"])
        total += 1
        t = tally.setdefault(cat, [0, 0])
        if verdict == "rejected":
            t[1] += 1
            if claim and claim not in seen_rej and len(rejected) < _MAX_EXAMPLES:
                seen_rej.add(claim)
                rejected.append((cat, claim))
        else:
            t[0] += 1
            if (
                verdict == "edited"
                and claim
                and claim not in seen_ed
                and len(edited) < _MAX_EXAMPLES
            ):
                seen_ed.add(claim)
                edited.append((cat, claim))
    if total < _MIN_DECISIONS:
        return ""

    lines = ["이 레포의 과거 리뷰에서 팀이 내린 판단(리뷰 보정 참고용):", ""]
    lines.append("카테고리별 수용/기각:")
    for cat, (acc, rej) in sorted(tally.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
        lines.append(f"- {cat}: 수용 {acc} · 기각 {rej}")
    if rejected:
        lines += ["", "팀이 자주 기각한 지적(이 레포에서 대체로 받아들이지 않는 유형):"]
        lines += [f"- [{cat}] {claim}" for cat, claim in rejected]
    if edited:
        lines += ["", "팀이 다듬어 수용한 지적(문구·범위를 조정해 반영):"]
        lines += [f"- [{cat}] {claim}" for cat, claim in edited]
    return "\n".join(lines)


def feedback_stats(rows) -> dict:
    """사람 판단 finding 행들을 카테고리별 수용/수정/기각 집계 + 대표 예시로 구조화(순수 함수).
    /learn 웹 탭 표시용 — LLM 주입(summarize_feedback)과 달리 최소 결정 수 게이트 없이 있는 그대로."""
    categories = {}  # category -> {approved, edited, rejected}
    approved, rejected, edited = [], [], []  # [{category, claim}], 중복 제거·최근순
    seen_app, seen_rej, seen_ed = set(), set(), set()
    total = 0
    for row in rows:
        cat = (row["category"] or "").strip() or "기타"
        claim = _one_line(row["claim"])
        verdict = _verdict(row["status"], row["edited_text"])
        total += 1
        c = categories.setdefault(cat, {"approved": 0, "edited": 0, "rejected": 0})
        c[verdict] += 1
        if verdict == "rejected":
            if claim and claim not in seen_rej and len(rejected) < _MAX_EXAMPLES:
                seen_rej.add(claim)
                rejected.append({"category": cat, "claim": claim})
        elif verdict == "edited":
            if claim and claim not in seen_ed and len(edited) < _MAX_EXAMPLES:
                seen_ed.add(claim)
                edited.append({"category": cat, "claim": claim})
        elif claim and claim not in seen_app and len(approved) < _MAX_EXAMPLES:
            seen_app.add(claim)
            approved.append({"category": cat, "claim": claim})
    ordered = sorted(
        categories.items(),
        key=lambda kv: -(kv[1]["approved"] + kv[1]["edited"] + kv[1]["rejected"]),
    )
    return {
        "total": total,
        "categories": [{"category": cat, **counts} for cat, counts in ordered],
        "approved_examples": approved,
        "rejected_examples": rejected,
        "edited_examples": edited,
    }


def repo_feedback_stats(conn, full_name) -> dict:
    """열린 커넥션으로 한 레포의 사람 판단을 조회해 구조화 통계 반환(/learn 탭 API용)."""
    rows = conn.execute(_QUERY, (full_name, _MAX_DECISIONS)).fetchall()
    return feedback_stats(rows)


_MAX_RECENT_DECISIONS = 10  # /learn '최근 결정 활동' 타임라인 표시 상한

# finding_decision 감사 이력 → 최근 사람 판단 이벤트(시간순). posted(시스템 게시)·pending 제외.
# fd.id DESC = 삽입 순(같은 초 decided_at 동률에도 안정적).
_RECENT_DECISIONS_QUERY = """
SELECT f.category AS category, f.claim AS claim,
       fd.to_status AS verdict, fd.decided_at AS decided_at, p.number AS pr_number
FROM finding_decision fd
JOIN finding f ON f.id = fd.finding_id
JOIN review_run rr ON rr.id = f.run_id
JOIN pull_request p ON p.id = rr.pr_id
JOIN repo r ON r.id = p.repo_id
WHERE r.full_name = ? COLLATE NOCASE
  AND fd.to_status IN ('approved', 'dismissed', 'edited')
ORDER BY fd.id DESC
LIMIT ?
"""


def recent_decisions(conn, full_name) -> list:
    """이 레포의 최근 사람 판단 이벤트(감사 이력 기반, 최근순)를 /learn 표시용으로 반환."""
    rows = conn.execute(
        _RECENT_DECISIONS_QUERY, (full_name, _MAX_RECENT_DECISIONS)
    ).fetchall()
    return [
        {
            "category": (r["category"] or "").strip() or "기타",
            "claim": _one_line(r["claim"]),
            "verdict": r["verdict"],
            "pr_number": r["pr_number"],
            "decided_at": (r["decided_at"] or "")[:16],
        }
        for r in rows
    ]


def db_feedback_source(*, db_path=None):
    """이 레포의 과거 사람 판단을 앱 DB에서 읽어 요약하는 feedback_source(req)->str 를 만든다.
    read-only SELECT + short-lived 커넥션(worker 진행 중에도 WAL 하에서 안전)."""
    path = db_path if db_path is not None else config.DB_PATH

    def source(req) -> str:
        conn = connect(path)
        try:
            rows = conn.execute(_QUERY, (req.repo, _MAX_DECISIONS)).fetchall()
        finally:
            conn.close()
        return summarize_feedback(rows)

    return source
