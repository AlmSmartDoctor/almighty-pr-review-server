import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server import config
from server.context import feedback_source, jira_keys
from server.db import connect, init_schema
from server.formatter import MARKER, build_comment, build_inline_comment
from server.github.gh import GhClient, GitHubCliError
from server.models import Finding
from server.repos import (
    finding_repo,
    job_repo,
    posted_repo,
    pr_repo,
    repo_repo,
    review_repo,
    settings_repo,
)
from server.review.diff_filter import commentable_lines
from server.review.harness import (
    HarnessProfile,
    create_harness,
    list_harnesses,
    validate_harness_name,
)


_initialized = False


def _ensure_schema():
    global _initialized
    if not _initialized:
        conn = connect(config.DB_PATH)
        init_schema(conn)
        conn.close()
        _initialized = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    from server.poller import poll_loop
    from server.worker import worker_loop

    _ensure_schema()
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(poll_loop(config.DB_PATH, stop_event=stop)),
        asyncio.create_task(worker_loop(config.DB_PATH, stop_event=stop)),
    ]
    try:
        yield
    finally:
        stop.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                print(f"[lifespan] background loop exited with error: {r!r}")


app = FastAPI(title="Almighty PR Review Server", lifespan=lifespan)


def get_conn():
    """★개정: 요청마다 커넥션 open/close. 전역 단일 커넥션 금지
    (sqlite3 check_same_thread + FastAPI 스레드풀 충돌 회피). WAL로 동시성 확보."""
    _ensure_schema()
    conn = connect(config.DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/models")
def list_models() -> dict:
    """설정 UI가 시작 시 주입받는 선택 가능한 모델·effort 목록(백엔드가 단일 소스).
    새 모델은 config.py만 갱신하면 프론트 수정 없이 드롭다운에 반영된다."""
    return {
        "claude": config.CLAUDE_MODELS,
        "codex": config.CODEX_MODELS,
        "claude_efforts": config.CLAUDE_EFFORTS,
        "codex_efforts": config.CODEX_EFFORTS,
    }


def _normalize_full_name(raw: str) -> str:
    """owner/repo만 남긴다: 공백·URL 접두어·후행 슬래시·.git 제거. 형태 불량이면 ValueError.
    정규화 없이 gh api /repos/{...}에 넘기면 이런 사소한 입력 오류가 404로 새어나온다."""
    name = (raw or "").strip()
    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "git@github.com:",
        "github.com/",
    ):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    name = name.strip("/")
    if name.endswith(".git"):
        name = name[:-4]
    parts = name.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("owner/repo 형식이 필요합니다")
    return name


class RepoIn(BaseModel):
    full_name: str
    local_path: str | None = None  # ★개정: worktree 소스 경로


@app.post("/api/repos", status_code=201)
def add_repo(body: RepoIn, conn=Depends(get_conn)):
    # local_path는 선택 — 비우면 리뷰 시 서비스 전용 clone에서 PR 브랜치를 체크아웃한다.
    # 모델/effort는 seed하지 않는다(NULL=전역 기본값 상속, 이후 레포별로 재정의 가능).
    try:
        full_name = _normalize_full_name(body.full_name)
    except ValueError:
        raise HTTPException(400, "owner/repo 형식으로 입력하세요.")
    rid = repo_repo.add(conn, full_name=full_name, local_path=body.local_path or None)
    return dict(repo_repo.get(conn, rid))


@app.get("/api/repos")
def list_repos(conn=Depends(get_conn)):
    return [dict(r) for r in conn.execute("SELECT * FROM repo").fetchall()]


@app.get("/api/overview")
def overview(conn=Depends(get_conn)):
    rows = conn.execute("""
      SELECT p.id, p.number, p.title, r.full_name AS repo,
             p.url, p.head_ref, p.body,
             p.author, p.created_at, p.first_seen_at, p.is_draft,
             (SELECT complexity FROM pre_screen ps WHERE ps.pr_id=p.id
                ORDER BY ps.id DESC LIMIT 1) AS prescreen,
             (SELECT duration_ms FROM pre_screen ps WHERE ps.pr_id=p.id
                ORDER BY ps.id DESC LIMIT 1) AS prescreen_duration_ms,
             (SELECT COUNT(*)
                FROM finding f JOIN review_run rr ON rr.id=f.run_id
                WHERE rr.pr_id=p.id) AS finding_count,
             (SELECT MIN(CASE f.severity WHEN 'critical' THEN 0 WHEN 'high'
                THEN 1 WHEN 'medium' THEN 2 ELSE 3 END)
                FROM finding f JOIN review_run rr ON rr.id=f.run_id
                WHERE rr.pr_id=p.id) AS sev_rank,
             (SELECT id FROM review_run rr WHERE rr.pr_id=p.id
                ORDER BY id DESC LIMIT 1) AS run_id,
             (SELECT status FROM review_run rr WHERE rr.pr_id=p.id
                ORDER BY id DESC LIMIT 1) AS run_status,
             (SELECT started_at FROM review_run rr WHERE rr.pr_id=p.id
                ORDER BY id DESC LIMIT 1) AS run_started_at,
             (SELECT finished_at FROM review_run rr WHERE rr.pr_id=p.id
                ORDER BY id DESC LIMIT 1) AS run_finished_at,
             (SELECT CASE
                       WHEN rr.started_at IS NULL THEN NULL
                       ELSE (strftime('%s', COALESCE(rr.finished_at, datetime('now'))) -
                             strftime('%s', rr.started_at)) * 1000
                     END
                FROM review_run rr WHERE rr.pr_id=p.id
                ORDER BY id DESC LIMIT 1) AS run_duration_ms,
             (SELECT error FROM review_run rr WHERE rr.pr_id=p.id
                ORDER BY id DESC LIMIT 1) AS run_error
      FROM pull_request p JOIN repo r ON r.id=p.repo_id
      WHERE p.state='open'
      ORDER BY
        COALESCE(datetime(p.created_at), datetime(p.first_seen_at)) DESC, p.id DESC
    """).fetchall()
    sev = {0: "critical", 1: "high", 2: "medium", 3: "low"}
    out = []
    for x in rows:
        row = dict(x)
        body = row.pop("body", None)  # jira 키 추출에만 쓰고 응답 본문엔 싣지 않음
        row["severity"] = sev.get(row["sev_rank"], "low")
        row["jira_links"] = _jira_links(row.get("head_ref"), row.get("title"), body)
        out.append(row)
    return out


def _jira_links(*texts: str | None) -> list[dict]:
    """PR 텍스트에서 찾은 Jira 키를 브라우저 링크로. base_url 미설정이면 [](링크 미노출)."""
    base = config.JIRA_BASE_URL.rstrip("/")
    if not base:
        return []
    return [
        {"key": k, "url": f"{base}/browse/{k}"} for k in jira_keys.find_keys(*texts)
    ]


@app.get("/api/learn")
def learn(conn=Depends(get_conn)):
    """레포별 팀 판단(수용·수정·기각) 집계 — /learn 탭이 열람하는 학습 신호.
    finding 사람 결정이 있는 레포만, 결정 수 많은 순으로 반환."""
    repos = conn.execute("SELECT full_name FROM repo ORDER BY full_name").fetchall()
    out = []
    for r in repos:
        stats = feedback_source.repo_feedback_stats(conn, r["full_name"])
        if not stats["total"]:
            continue
        out.append(
            {
                "repo": r["full_name"],
                **stats,
                "recent_decisions": feedback_source.recent_decisions(
                    conn, r["full_name"]
                ),
            }
        )
    out.sort(key=lambda x: (-x["total"], x["repo"]))
    return out


class RepoPatch(BaseModel):
    enabled: int | None = None
    trigger_mode: str | None = None
    claude_model: str | None = None
    claude_effort: str | None = None
    codex_model: str | None = None
    codex_effort: str | None = None
    vendor_claude_on: int | None = None
    vendor_codex_on: int | None = None
    merge_enabled: int | None = None
    harness_name: str | None = None
    local_path: str | None = None  # ★개정
    context_static_on: int | None = None
    context_jira_on: int | None = None
    context_db_schema_on: int | None = None
    context_graphify_on: int | None = None
    context_feedback_on: int | None = None
    verify_singles_on: int | None = None
    incremental_review_on: int | None = None
    static_context_path: str | None = None
    jira_project_keys: str | None = None
    db_schema_path: str | None = None
    graphify_path: str | None = None


@app.patch("/api/repos/{rid}")
def patch_repo(rid: int, body: RepoPatch, conn=Depends(get_conn)):
    fields = body.model_dump(exclude_none=True)
    # None이 '상속(전역 기본값)으로 되돌림'을 뜻하는 필드는 exclude_none에서 되살린다.
    for key in (
        "context_static_on",
        "context_jira_on",
        "context_db_schema_on",
        "context_graphify_on",
        "context_feedback_on",
        "verify_singles_on",
        "incremental_review_on",
        "claude_model",
        "claude_effort",
        "codex_model",
        "codex_effort",
    ):
        if key in body.model_fields_set and getattr(body, key) is None:
            fields[key] = None
    repo_repo.update(conn, rid, **fields)
    return dict(repo_repo.get(conn, rid))


@app.get("/api/settings")
def get_settings(conn=Depends(get_conn)):
    return dict(settings_repo.get(conn))


class SettingsPatch(BaseModel):
    default_effort: str | None = None
    claude_effort: str | None = None
    codex_effort: str | None = None
    concurrency_limit: int | None = None
    default_poll_interval: int | None = None
    prescreen_model: str | None = None
    review_model: str | None = None
    codex_model: str | None = None
    prescreen_gate_threshold: str | None = None
    context_static_on: int | None = None
    context_jira_on: int | None = None
    context_db_schema_on: int | None = None
    context_graphify_on: int | None = None
    context_feedback_on: int | None = None
    verify_singles_on: int | None = None
    incremental_review_on: int | None = None


@app.patch("/api/settings")
def patch_settings(body: SettingsPatch, conn=Depends(get_conn)):
    settings_repo.update(conn, **body.model_dump(exclude_none=True))
    return dict(settings_repo.get(conn))


@app.get("/api/runs/{run_id}/findings")
def run_findings(run_id: int, conn=Depends(get_conn)):
    return [dict(f) for f in finding_repo.list_for_run(conn, run_id)]


@app.get("/api/runs/{run_id}/vendor-results")
def run_vendor_results(run_id: int, conn=Depends(get_conn)):
    # ★개정 (codex v6 [MEDIUM]): 실패 벤더 노출용. 프론트 ReviewSection이
    # status='failed' 벤더에 배지를 띄워 부분 실패를 사용자에게 알린다.
    return [dict(v) for v in review_repo.list_vendor_results(conn, run_id)]


@app.post("/api/runs/{run_id}/retry-vendors", status_code=202)
def retry_vendors(run_id: int, conn=Depends(get_conn)):
    """부분 실패한 리뷰의 **실패 벤더만** 재실행(새 run 미생성, 기존 성공분 보존).
    head가 갱신됐거나 실패 벤더가 없거나 부분 실패 상태(done)가 아니면 거절한다."""
    run = review_repo.get_run(conn, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    pr = pr_repo.get(conn, run["pr_id"])
    if pr is None:
        raise HTTPException(404, "pr not found")
    if pr["head_sha"] != run["head_sha"]:
        raise HTTPException(409, "PR가 갱신됨 — 전체 재리뷰를 사용하세요")
    if run["status"] != "done":
        raise HTTPException(
            409, "부분 실패한 리뷰에만 사용 가능 — 전체 재리뷰를 사용하세요"
        )
    # 실패했더라도 현재 비활성인 벤더는 재시도해도 worker가 걸러 무동작이므로 제외한다.
    repo = repo_repo.get(conn, pr["repo_id"])
    enabled = {"claude"} if repo["vendor_claude_on"] else set()
    if repo["vendor_codex_on"]:
        enabled.add("codex")
    if not (set(review_repo.failed_vendors(conn, run_id)) & enabled):
        raise HTTPException(409, "재시도할 실패 벤더가 없습니다")
    job_id = job_repo.enqueue_retry(
        conn, pr_id=run["pr_id"], head_sha=run["head_sha"], run_id=run_id
    )
    return {"job_id": job_id}


@app.get("/api/runs/{run_id}/context")
def run_context(run_id: int, conn=Depends(get_conn)):
    run = review_repo.get_run(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    meta = json.loads(run["context_meta"]) if run["context_meta"] else None
    return {"text": run["context_text"] or "", "meta": meta}


class FindingPatch(BaseModel):
    status: str
    edited_text: str | None = None


@app.patch("/api/findings/{fid}")
def patch_finding(fid: int, body: FindingPatch, conn=Depends(get_conn)):
    # edited_text 미제공(None) 시 set_status에 넘기지 않는다 — 넘기면 sentinel이
    # 아니라 명시적 None으로 처리돼 기존 edited_text가 NULL로 덮인다(데이터 손실).
    if body.edited_text is None:
        finding_repo.set_status(conn, fid, body.status)
    else:
        finding_repo.set_status(conn, fid, body.status, body.edited_text)
    return dict(finding_repo.get(conn, fid))


_gh = None


def get_gh():
    global _gh
    if _gh is None:
        _gh = GhClient()
    return _gh


def _github_error_message(error: GitHubCliError) -> str:
    if error.http_status == 401:
        return "GitHub 인증이 필요합니다. GH_TOKEN, GITHUB_TOKEN, GITHUB_PERSONAL_ACCESS_TOKEN 중 하나를 확인하세요."
    if error.http_status == 403:
        return "GitHub 권한이 부족하거나 SSO 승인이 필요합니다."
    if error.http_status == 404:
        # 404는 어느 preflight에서 났는지로 갈라 원인을 좁혀준다(GitHub은 접근권 없는
        # 비공개 레포도 존재를 숨기려 404를 준다 → repo-404는 접근권/SSO도 함께 안내).
        if error.command_kind == "preflight_issue":
            return "해당 PR을 찾을 수 없습니다. PR 번호와 레포가 맞는지 확인하세요."
        if error.command_kind in ("preflight_repo", "clone", "diff", "compare_diff"):
            return (
                "레포를 찾을 수 없거나 이 토큰으로 접근할 수 없습니다. "
                "레포 이름 오타·비공개 접근 권한·조직 SSO 승인 여부를 확인하세요."
            )
        return "GitHub repo 또는 PR에 접근할 수 없습니다."
    return error.message


def _health_status_code(health: dict) -> int:
    # 실패 섹션이면 인식된 HTTP 상태(401/403/404)는 그대로, 그 외(네트워크·미로그인·
    # 5xx 등 http_status 미상)는 502로. ok 여부로 판정해 status가 None이어도 200으로
    # 새지 않게 한다(_post_health는 첫 실패 섹션에서 즉시 반환하므로 iteration 순서상
    # 첫 not-ok 섹션이 곧 실패 지점).
    for section in ("auth", "repo", "issue"):
        sec = health[section]
        if not sec.get("ok"):
            status = sec.get("status")
            return status if status in {401, 403, 404} else 502
    return 200


def _post_health(conn, pid: int, gh) -> dict:
    pr = pr_repo.get(conn, pid)
    repo = repo_repo.get(conn, pr["repo_id"])
    health = {
        "ok": False,
        "auth": {"ok": False, "login": None, "error": None},
        "repo": {"ok": False, "full_name": repo["full_name"], "error": None},
        "issue": {"ok": False, "number": pr["number"], "error": None},
        "message": "",
    }
    try:
        user = gh.preflight_user()
        health["auth"].update({"ok": True, "login": user.get("login")})
    except GitHubCliError as e:
        health["auth"].update(
            {"error": _github_error_message(e), "status": e.http_status}
        )
        health["message"] = health["auth"]["error"]
        return health

    try:
        repo_info = gh.preflight_repo(repo["full_name"])
        health["repo"].update(
            {"ok": True, "full_name": repo_info.get("full_name", repo["full_name"])}
        )
    except GitHubCliError as e:
        health["repo"].update(
            {"error": _github_error_message(e), "status": e.http_status}
        )
        health["message"] = health["repo"]["error"]
        return health

    try:
        issue = gh.preflight_issue(repo["full_name"], pr["number"])
        health["issue"].update(
            {"ok": True, "number": issue.get("number", pr["number"])}
        )
    except GitHubCliError as e:
        health["issue"].update(
            {"error": _github_error_message(e), "status": e.http_status}
        )
        health["message"] = health["issue"]["error"]
        return health

    health["ok"] = True
    health["message"] = "GitHub 포스팅 가능"
    return health


POSTABLE_FINDING_STATUSES = {"approved", "edited"}
# 코멘트 본문에 실을 상태 집합 — 이미 게시된(posted) finding도 포함해야 같은 run을
# 증분 승인하며 재포스팅(in-place edit)할 때 앞서 게시한 지적이 사라지지 않는다.
DISPLAYED_FINDING_STATUSES = POSTABLE_FINDING_STATUSES | {"posted"}


def _row_to_finding(f) -> Finding:
    return Finding(
        f["vendor"],
        f["file"],
        f["line"],
        f["severity"],
        f["category"],
        f["edited_text"] or f["claim"],
        f["rationale"],
        f["confidence"] or 0.0,
    )


def _comments_to_post(conn, run_id: int):
    """게시 대상 벤더별 코멘트. 새로 승인/수정된(approved/edited) finding이 있는 벤더만
    대상으로 하되, 본문은 이미 posted된 것까지 합쳐 구성한다(재포스팅 유실 방지).
    반환 항목: {vendor, body, new_ids(이번에 posted로 전환), all_ids(코멘트 전체)}."""
    rows = finding_repo.list_for_run(conn, run_id)
    pending = [f for f in rows if f["status"] in POSTABLE_FINDING_STATUSES]
    display = [f for f in rows if f["status"] in DISPLAYED_FINDING_STATUSES]
    order: list[str] = []
    for f in pending:  # 벤더 등장 순서 보존(부분 실패 응답의 posted 순서 안정성)
        if f["vendor"] not in order:
            order.append(f["vendor"])
    out = []
    for vendor in order:
        findings = [_row_to_finding(f) for f in display if f["vendor"] == vendor]
        out.append(
            {
                "vendor": vendor,
                "body": build_comment(vendor=vendor, findings=findings),
                "findings": findings,  # review 인라인 코멘트용(diff 라인에 붙임)
                "new_ids": [f["id"] for f in pending if f["vendor"] == vendor],
                "all_ids": [f["id"] for f in display if f["vendor"] == vendor],
            }
        )
    return out


@app.get("/api/runs/{run_id}/post-preview")
def post_preview(run_id: int, conn=Depends(get_conn)):
    return {
        "comments": [
            {"vendor": c["vendor"], "body": c["body"]}
            for c in _comments_to_post(conn, run_id)
        ]
    }


@app.get("/api/prs/{pid}/post-health")
def post_health(pid: int, conn=Depends(get_conn), gh=Depends(get_gh)):
    health = _post_health(conn, pid, gh)
    return JSONResponse(status_code=_health_status_code(health), content=health)


def _inline_comments(gh, repo_full: str, number: int, findings):
    """diff에 매핑되는 finding만 review 인라인 코멘트로 변환. createReview는 diff 밖
    라인이 하나라도 있으면 전체를 422로 거부하므로 유효 라인만 통과시킨다. diff 조회가
    실패하면 인라인 없이 본문만으로 게시(게시 자체는 막지 않는다)."""
    try:
        diff = gh.diff(repo_full, number)
    except GitHubCliError:
        return []
    valid = commentable_lines(diff)
    return [
        {"path": f.file, "line": f.line, "body": build_inline_comment(f)}
        for f in findings
        if f.line and f.line in valid.get(f.file, set())
    ]


def _create_review(gh, repo, pr, run, body: str, findings) -> dict:
    comments = _inline_comments(gh, repo["full_name"], pr["number"], findings)
    return gh.create_review(
        repo["full_name"], pr["number"], run["head_sha"], body, comments
    )


@app.post("/api/runs/{run_id}/post")
def post_run(run_id: int, conn=Depends(get_conn), gh=Depends(get_gh)):
    run = review_repo.get_run(conn, run_id)
    pr = pr_repo.get(conn, run["pr_id"])
    repo = repo_repo.get(conn, pr["repo_id"])
    health = _post_health(conn, pr["id"], gh)
    if not health["ok"]:
        return JSONResponse(
            status_code=_health_status_code(health),
            content={"detail": health},
        )
    posted = []
    for comment in _comments_to_post(conn, run_id):
        vendor = comment["vendor"]
        body = comment["body"]
        # update-or-create를 PR review 프리미티브로: 같은 PR·벤더의 기존 review가 있으면
        # 본문만 PUT(pull_request_review.edited → PR 봇 무시 → 재게시 무알림), 없으면
        # create(첫 게시 → 리뷰+인라인 → PR 봇 Slack 알림). 레거시 issue comment 행은
        # review로 이어받지 못하므로 supersede하고 review를 새로 생성한다.
        prev = posted_repo.latest_for_pr_vendor(conn, pr_id=pr["id"], vendor=vendor)
        reuse = (
            prev is not None and prev["kind"] == "review" and prev["github_comment_id"]
        )
        try:
            if reuse:
                try:
                    res = gh.update_review(
                        repo["full_name"],
                        pr["number"],
                        prev["github_comment_id"],
                        body,
                    )
                except GitHubCliError as e:
                    if e.http_status != 404:
                        raise
                    # review가 GitHub에서 사라짐(dismiss/삭제) → 새로 생성 폴백.
                    res = _create_review(gh, repo, pr, run, body, comment["findings"])
                posted_repo.supersede(conn, prev["id"])
            else:
                res = _create_review(gh, repo, pr, run, body, comment["findings"])
                if prev is not None:  # 레거시 issue comment 행 정리
                    posted_repo.supersede(conn, prev["id"])
        except GitHubCliError as e:
            return JSONResponse(
                status_code=502,
                content={
                    "detail": {
                        "message": _github_error_message(e),
                        "posted": posted,
                        "failed": {
                            "vendor": vendor,
                            "error": _github_error_message(e),
                            "command_kind": e.command_kind,
                        },
                    }
                },
            )
        posted_repo.add(
            conn,
            run_id=run_id,
            vendor=vendor,
            github_comment_id=str(res["id"]),
            url=res["html_url"],
            marker=MARKER.format(vendor=vendor),
            body=body,
            head_sha=run["head_sha"],
            finding_ids=comment["all_ids"],
            kind="review",
        )
        for fid in comment["new_ids"]:  # 이번에 새로 게시한 것만 posted 전환
            finding_repo.set_status(conn, fid, "posted")
        posted.append({"vendor": vendor, "url": res["html_url"]})
    return {"posted": posted}


@app.post("/api/prs/{pid}/review", status_code=202)
def trigger_review(pid: int, conn=Depends(get_conn)):
    pr = pr_repo.get(conn, pid)
    if pr is None:
        raise HTTPException(404, "pr not found")
    job_id = job_repo.enqueue_manual(conn, pr_id=pid, head_sha=pr["head_sha"])
    return {"job_id": job_id}


@app.post("/api/webhooks/github")
async def github_webhook(request: Request, conn=Depends(get_conn)):
    """GitHub pull_request 웹훅 수신 → poller와 동일 로직으로 리뷰 job enqueue.
    HMAC 검증(env-only 시크릿) 후 auto 레포·벤더·needs_review 게이트를 그대로 적용한다."""
    from server.github.webhook import parse_pull_request_event, verify_signature

    body = await request.body()
    if not config.GITHUB_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    if not verify_signature(
        config.GITHUB_WEBHOOK_SECRET, body, request.headers.get("X-Hub-Signature-256")
    ):
        raise HTTPException(status_code=401, detail="invalid signature")
    if request.headers.get("X-GitHub-Event") != "pull_request":
        return {"status": "ignored"}  # ping/기타 이벤트
    info = parse_pull_request_event(body)
    if info is None:
        return {"status": "ignored"}  # 리뷰 대상 action 아님/형태 불일치
    repo = repo_repo.get_by_full_name(conn, info["full_name"])
    if repo is None or not repo["enabled"] or repo["trigger_mode"] != "auto":
        return {
            "status": "ignored"
        }  # 미등록/비활성/manual = 자동 리뷰 안 함(poller 동일)
    pid = pr_repo.upsert(
        conn,
        repo_id=repo["id"],
        number=info["number"],
        title=info["title"],
        author=info["author"],
        head_sha=info["head_sha"],
        base_ref=info["base_ref"],
        url=info["url"],
        state=info["state"],
        head_ref=info["head_ref"],
        body=info["body"],
        is_draft=info["is_draft"],
    )
    has_vendor = repo["vendor_claude_on"] or repo["vendor_codex_on"]
    if has_vendor and pr_repo.needs_review(conn, pid):
        job_repo.enqueue(conn, pr_id=pid, head_sha=info["head_sha"], trigger="auto")
        return {"status": "enqueued", "pr": info["number"]}
    return {"status": "skipped", "pr": info["number"]}


@app.get("/api/harness")
def list_harness():
    return {"harnesses": list_harnesses()}


def _valid_name_or_400(name: str) -> None:
    try:
        validate_harness_name(name)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid harness name")


@app.get("/api/harness/{name}")
def get_harness(name: str):
    _valid_name_or_400(name)
    if not (config.HARNESS_DIR / name).is_dir():
        raise HTTPException(status_code=404, detail="harness not found")
    hp = HarnessProfile.load(name)
    return {
        "name": hp.name,
        "system_prompt": hp.system_prompt,
        "claude_allowed_tools": hp.claude_allowed_tools,
        "codex_sandbox": hp.codex_sandbox,
    }


class HarnessPut(BaseModel):
    system_prompt: str | None = None


@app.put("/api/harness/{name}")
def put_harness(name: str, body: HarnessPut):
    _valid_name_or_400(name)
    base = config.HARNESS_DIR / name
    if not base.is_dir():
        create_harness(name, system_prompt=body.system_prompt)
    elif body.system_prompt is not None:
        (base / "review-system-prompt.md").write_text(body.system_prompt)
    return get_harness(name)
