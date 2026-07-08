import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from server import config
from server.db import connect, init_schema
from server.formatter import MARKER, build_comment
from server.github.gh import GhClient
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
from server.review.harness import HarnessProfile


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


class RepoIn(BaseModel):
    full_name: str
    local_path: str | None = None  # ★개정: worktree 소스 경로


@app.post("/api/repos", status_code=201)
def add_repo(body: RepoIn, conn=Depends(get_conn)):
    rid = repo_repo.add(conn, full_name=body.full_name, local_path=body.local_path)
    return dict(repo_repo.get(conn, rid))


@app.get("/api/repos")
def list_repos(conn=Depends(get_conn)):
    return [dict(r) for r in conn.execute("SELECT * FROM repo").fetchall()]


@app.get("/api/overview")
def overview(conn=Depends(get_conn)):
    rows = conn.execute("""
      SELECT p.id, p.number, p.title, r.full_name AS repo,
             (SELECT complexity FROM pre_screen ps WHERE ps.pr_id=p.id
                ORDER BY ps.id DESC LIMIT 1) AS prescreen,
             (SELECT MIN(CASE f.severity WHEN 'critical' THEN 0 WHEN 'high'
                THEN 1 WHEN 'medium' THEN 2 ELSE 3 END)
                FROM finding f JOIN review_run rr ON rr.id=f.run_id
                WHERE rr.pr_id=p.id) AS sev_rank,
             (SELECT id FROM review_run rr WHERE rr.pr_id=p.id
                ORDER BY id DESC LIMIT 1) AS run_id
      FROM pull_request p JOIN repo r ON r.id=p.repo_id
      WHERE p.state='open' ORDER BY p.updated_at DESC
    """).fetchall()
    sev = {0: "critical", 1: "high", 2: "medium", 3: "low"}
    return [{**dict(x), "severity": sev.get(x["sev_rank"], "low")} for x in rows]


class RepoPatch(BaseModel):
    enabled: int | None = None
    trigger_mode: str | None = None
    default_effort: str | None = None
    vendor_claude_on: int | None = None
    vendor_codex_on: int | None = None
    merge_enabled: int | None = None
    auto_post: int | None = None
    harness_name: str | None = None
    local_path: str | None = None  # ★개정


@app.patch("/api/repos/{rid}")
def patch_repo(rid: int, body: RepoPatch, conn=Depends(get_conn)):
    repo_repo.update(conn, rid, **body.model_dump(exclude_none=True))
    return dict(repo_repo.get(conn, rid))


@app.get("/api/settings")
def get_settings(conn=Depends(get_conn)):
    return dict(settings_repo.get(conn))


class SettingsPatch(BaseModel):
    default_effort: str | None = None
    concurrency_limit: int | None = None
    default_poll_interval: int | None = None
    approval_gate_on: int | None = None
    prescreen_model: str | None = None
    prescreen_gate_threshold: str | None = None


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


@app.post("/api/runs/{run_id}/post")
def post_run(run_id: int, conn=Depends(get_conn), gh=Depends(get_gh)):
    run = review_repo.get_run(conn, run_id)
    pr = pr_repo.get(conn, run["pr_id"])
    repo = repo_repo.get(conn, pr["repo_id"])
    rows = [
        f for f in finding_repo.list_for_run(conn, run_id) if f["status"] == "approved"
    ]
    posted = []
    by_vendor: dict[str, list[Finding]] = {}
    for f in rows:
        by_vendor.setdefault(f["vendor"], []).append(
            Finding(
                f["vendor"],
                f["file"],
                f["line"],
                f["severity"],
                f["category"],
                f["edited_text"] or f["claim"],
                f["rationale"],
                f["confidence"] or 0.0,
            )
        )
    for vendor, findings in by_vendor.items():
        body = build_comment(vendor=vendor, findings=findings)
        # ★개정(codex 재검증 [MEDIUM]): 진짜 update-or-create.
        # 같은 PR·벤더의 기존 비대체 코멘트가 있으면 GitHub상 in-place 수정,
        # 없으면 새로 post. → GitHub 화면에도 중복이 남지 않음.
        prev = posted_repo.latest_for_pr_vendor(conn, pr_id=pr["id"], vendor=vendor)
        fids = [f["id"] for f in rows if f["vendor"] == vendor]
        if prev is not None and prev["github_comment_id"]:
            res = gh.edit_comment(repo["full_name"], prev["github_comment_id"], body)
            posted_repo.supersede(conn, prev["id"])
        else:
            res = gh.post_comment(repo["full_name"], pr["number"], body)
        # ★개정 (codex v3 [LOW]): API JSON의 .id를 그대로 저장(URL 파싱 제거).
        posted_repo.add(
            conn,
            run_id=run_id,
            vendor=vendor,
            github_comment_id=str(res["id"]),
            url=res["html_url"],
            marker=MARKER.format(vendor=vendor),
            body=body,
            head_sha=run["head_sha"],
            finding_ids=fids,
        )
        for f in rows:
            if f["vendor"] == vendor:
                finding_repo.set_status(conn, f["id"], "posted")
        posted.append({"vendor": vendor, "url": res["html_url"]})
    return {"posted": posted}


@app.post("/api/prs/{pid}/review", status_code=202)
def trigger_review(pid: int, conn=Depends(get_conn)):
    pr = pr_repo.get(conn, pid)
    job_id = job_repo.enqueue(
        conn, pr_id=pid, head_sha=pr["head_sha"], trigger="manual"
    )
    return {"job_id": job_id}


@app.get("/api/harness/{name}")
def get_harness(name: str):
    hp = HarnessProfile.load(name)
    return {
        "name": hp.name,
        "system_prompt": hp.system_prompt,
        "claude_allowed_tools": hp.claude_allowed_tools,
        "codex_sandbox": hp.codex_sandbox,
        "model": hp.model,
        "effort": hp.effort,
    }


class HarnessPut(BaseModel):
    system_prompt: str | None = None


@app.put("/api/harness/{name}")
def put_harness(name: str, body: HarnessPut):
    base = config.HARNESS_DIR / name
    if body.system_prompt is not None:
        (base / "review-system-prompt.md").write_text(body.system_prompt)
    return get_harness(name)
