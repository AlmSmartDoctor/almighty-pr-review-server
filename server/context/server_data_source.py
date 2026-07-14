from server import config
from server.db import connect

_MAX_OPEN_FINDINGS = 200  # 스캔 상한(비용)
_MAX_EXAMPLES = 8  # 대표 미결 지적 예시 상한(중복 제거)
_MAX_CLAIM_CHARS = 160  # 예시 claim(·PR 제목) 1줄 절단
_MAX_OPEN_PRS = 30  # 동시 진행 오픈 PR 목록 상한

# 다른 열린 PR들의 미결(status='pending') finding — 현재 리뷰 중인 PR은 자기-에코 방지로 제외.
# finding엔 repo_id가 없어 review_run→pull_request→repo 조인. done 런의 미결만 보되,
# (PR, file, line, claim)로 중복 제거한다 — 전체 재리뷰가 같은 지적을 여러 done 런에 다시
# 실어도 1건으로 합치고, 증분 리뷰가 델타만 훑어 이전 런의 미결을 다시 싣지 않아도 그 미결은
# 보존한다("최신 done 런만" 필터는 후자를 통째로 누락시켜 미사용).
_OPEN_FINDINGS_QUERY = """
SELECT f.category AS category, f.severity AS severity,
       f.claim AS claim, p.number AS pr_number
FROM finding f
JOIN review_run rr ON rr.id = f.run_id
JOIN pull_request p ON p.id = rr.pr_id
JOIN repo r ON r.id = p.repo_id
WHERE r.full_name = ? COLLATE NOCASE
  AND p.state = 'open'
  AND p.number != ?
  AND f.status = 'pending'
  AND rr.status = 'done'
GROUP BY p.id, f.file, f.line, f.claim
ORDER BY p.number DESC, MAX(f.id) DESC
LIMIT ?
"""

# 같은 레포에 현재 열려 있는 다른 PR(리뷰 중인 PR 제외) — 동시 진행 작업 상황 인지용.
_OPEN_PRS_QUERY = """
SELECT p.number AS number, p.title AS title, p.author AS author
FROM pull_request p
JOIN repo r ON r.id = p.repo_id
WHERE r.full_name = ? COLLATE NOCASE
  AND p.state = 'open'
  AND p.number != ?
ORDER BY p.number DESC
LIMIT ?
"""


def _one_line(claim: str) -> str:
    return " ".join((claim or "").split())[:_MAX_CLAIM_CHARS]


def summarize_open_findings(rows) -> str:
    """오픈 PR들의 미결(pending) 지적을 카테고리별 건수 + 대표 예시로 요약(순수 함수)."""
    tally = {}  # category -> count
    examples = []  # (severity, category, claim) 중복 제거·순서 유지
    seen = set()
    prs = set()
    total = 0
    for row in rows:
        cat = (row["category"] or "").strip() or "기타"
        sev = (row["severity"] or "").strip() or "?"
        claim = _one_line(row["claim"])
        total += 1
        prs.add(row["pr_number"])
        tally[cat] = tally.get(cat, 0) + 1
        if claim and claim not in seen and len(examples) < _MAX_EXAMPLES:
            seen.add(claim)
            examples.append((sev, cat, claim))
    if total == 0:
        return ""

    lines = [
        f"이 레포에서 아직 처리되지 않은(미결) 리뷰 지적 — 다른 열린 PR {len(prs)}건의 "
        f"최신 리뷰 기준(중복 제기 방지·일관성 참고용):",
        "",
        "카테고리별 미결 건수:",
    ]
    for cat, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {cat}: {n}")
    if examples:
        lines += ["", "대표 미결 지적:"]
        lines += [f"- [{sev}/{cat}] {claim}" for sev, cat, claim in examples]
    return "\n".join(lines)


def open_findings_source(*, db_path=None):
    """오픈 PR들의 미결 지적을 앱 DB에서 읽어 요약하는 graph_source(req)->str 를 만든다.
    read-only SELECT + short-lived 커넥션. 현재 PR(req.pr_number)은 제외."""
    path = db_path if db_path is not None else config.DB_PATH

    def source(req) -> str:
        conn = connect(path)
        try:
            rows = conn.execute(
                _OPEN_FINDINGS_QUERY, (req.repo, req.pr_number, _MAX_OPEN_FINDINGS)
            ).fetchall()
        finally:
            conn.close()
        return summarize_open_findings(rows)

    return source


def summarize_open_prs(rows) -> str:
    """같은 레포의 다른 오픈 PR 목록을 번호·제목·작성자로 요약(순수 함수)."""
    lines = [
        f"- #{row['number']} {_one_line(row['title']) or '(제목 없음)'} "
        f"({(row['author'] or '').strip() or '?'})"
        for row in rows
    ]
    if not lines:
        return ""
    return "이 레포에 현재 열려 있는 다른 PR (동시 진행 작업 인지용):\n" + "\n".join(
        lines
    )


def open_prs_source(*, db_path=None):
    """같은 레포의 다른 열린 PR 목록을 앱 DB에서 읽어 요약하는 graph_source(req)->str.
    read-only SELECT + short-lived 커넥션. 현재 PR(req.pr_number)은 제외."""
    path = db_path if db_path is not None else config.DB_PATH

    def source(req) -> str:
        conn = connect(path)
        try:
            rows = conn.execute(
                _OPEN_PRS_QUERY, (req.repo, req.pr_number, _MAX_OPEN_PRS)
            ).fetchall()
        finally:
            conn.close()
        return summarize_open_prs(rows)

    return source


# 이 레포의 리뷰 실행 활동 — 처리량·최근성·실패 이력만(프로젝트 진행 맥락). finding 내용/심각도
# 분포는 의도적으로 배제(#2 오픈 finding·#8 팀 피드백과 겹치지 않고 앵커링 위험 없음). 단일 집계 행.
_ACTIVITY_QUERY = """
SELECT
  SUM(CASE WHEN rr.status='done' THEN 1 ELSE 0 END) AS done_runs,
  COUNT(DISTINCT CASE WHEN rr.status='done' THEN rr.pr_id END) AS reviewed_prs,
  MAX(CASE WHEN rr.status='done' THEN rr.finished_at END) AS last_done,
  SUM(CASE WHEN rr.status='failed' THEN 1 ELSE 0 END) AS failed_runs
FROM review_run rr
JOIN pull_request p ON p.id = rr.pr_id
JOIN repo r ON r.id = p.repo_id
WHERE r.full_name = ? COLLATE NOCASE
"""


def summarize_activity(row) -> str:
    """레포 리뷰 실행 활동 요약(순수 함수). 완료·실패 이력이 전무하면 ""(미주입)."""
    done = (row["done_runs"] if row else 0) or 0
    prs = (row["reviewed_prs"] if row else 0) or 0
    failed = (row["failed_runs"] if row else 0) or 0
    last = ((row["last_done"] if row else "") or "")[:16]
    if not done and not failed:
        return ""
    lines = ["이 레포 리뷰 활동 현황 (프로젝트 진행 맥락):"]
    if done:
        tail = f", 마지막 리뷰 {last}" if last else ""
        lines.append(f"- 완료된 리뷰 {done}건 (PR {prs}건){tail}")
    else:
        lines.append("- 아직 완료된 리뷰 없음")
    if failed:
        lines.append(f"- 리뷰 실패 이력 {failed}건")
    return "\n".join(lines)


def activity_source(*, db_path=None):
    """이 레포 리뷰 실행 활동을 앱 DB에서 읽어 요약하는 graph_source(req)->str.
    read-only 단일 집계 SELECT + short-lived 커넥션."""
    path = db_path if db_path is not None else config.DB_PATH

    def source(req) -> str:
        conn = connect(path)
        try:
            row = conn.execute(_ACTIVITY_QUERY, (req.repo,)).fetchone()
        finally:
            conn.close()
        return summarize_activity(row)

    return source
