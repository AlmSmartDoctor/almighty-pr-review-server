from fastapi import Depends, FastAPI
from pydantic import BaseModel

from server import config
from server.db import connect, init_schema
from server.repos import (
    finding_repo,
    repo_repo,
    review_repo,
    settings_repo,
)

app = FastAPI(title="Almighty PR Review Server")

_initialized = False


def _ensure_schema():
    global _initialized
    if not _initialized:
        conn = connect(config.DB_PATH)
        init_schema(conn)
        conn.close()
        _initialized = True


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
