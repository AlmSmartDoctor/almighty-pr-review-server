import asyncio
import json
import re
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server import config
from server.context import feedback_source, jira_keys
from server.context.base import redact_secrets
from server.context.registry import _effective
from server.db import connect, init_schema
from server.formatter import MARKER, build_comment, build_inline_comment
from server.github.gh import GhClient, GitHubCliError
from server.http_security import protect_management_api
from server.models import Finding
from server.repos import (
    feedback_repo,
    finding_repo,
    job_repo,
    post_operation_repo,
    posted_repo,
    pr_repo,
    repo_repo,
    review_repo,
    review_rule_repo,
    settings_repo,
    wiki_repo,
)
from server.review.diff_filter import commentable_lines
from server.review.finding_policy import (
    policy_snapshot_from_row,
    resolve_policy_snapshot,
)
from server.review.prescreen import THRESHOLDS, is_valid_prescreen_model
from server.routes.harness import (
    HarnessPut,
    _valid_name_or_400,
    get_harness,
    list_harness,
    put_harness,
    router as harness_router,
)


_initialized = False
_POST_LOCKS: dict[int, threading.Lock] = {}
_POST_LOCKS_GUARD = threading.Lock()


def _post_lock(run_id: int) -> threading.Lock:
    with _POST_LOCKS_GUARD:
        return _POST_LOCKS.setdefault(run_id, threading.Lock())


def _ensure_schema():
    global _initialized
    if not _initialized:
        conn = connect(config.DB_PATH)
        init_schema(conn)
        conn.close()
        _initialized = True


def _diagnostic_cleanup_coro(stop_event):
    if not config.DIAGNOSTIC_CLEANUP_ENABLED:
        return None
    from server.retention import diagnostic_cleanup_loop

    return diagnostic_cleanup_loop(
        config.DB_PATH,
        retention_days=config.DIAGNOSTIC_RETENTION_DAYS,
        raw_dir=config.RAW_DIR,
        stop_event=stop_event,
        context_retention_days=config.CONTEXT_PAYLOAD_RETENTION_DAYS,
    )


class BackgroundShutdownError(RuntimeError):
    pass


async def _shutdown_background_tasks(tasks, stop_event) -> list:
    stop_event.set()
    if not tasks:
        return []
    _done, pending = await asyncio.wait(
        tasks, timeout=config.BACKGROUND_SHUTDOWN_GRACE_SEC
    )
    for task in pending:
        task.cancel()
    if pending:
        _cleaned, pending = await asyncio.wait(
            pending, timeout=config.BACKGROUND_CLEANUP_TIMEOUT_SEC
        )
    if pending:
        for task in pending:
            task.cancel()
        names = sorted(task.get_name() for task in pending)
        raise BackgroundShutdownError(
            f"background cleanup deadline exceeded: {','.join(names)}"
        )
    # Every task is already terminal, so gather cannot extend the deadline.
    return await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from server.pipeline import _safe_concurrency_limit
    from server.poller import poll_loop
    from server.review.runner import RunnerPool
    from server.worker import (
        lease_heartbeat_loop,
        recover_worker_state,
        worker_loop,
    )
    from server.repos import process_repo

    _ensure_schema()
    # review job lane은 최대 2개로 병렬화하되 vendor CLI 총동시성은 기존 전역 설정을
    # 공유 RunnerPool로 그대로 지킨다. stale 복구는 lane 시작 전에 단 한 번 수행한다.
    startup = connect(config.DB_PATH)
    try:
        vendor_limit = _safe_concurrency_limit(
            settings_repo.get(startup)["concurrency_limit"]
        )
    finally:
        startup.close()
    process_id = uuid.uuid4().hex
    lease = connect(config.DB_PATH)
    try:
        process_repo.register(lease, process_id)
    finally:
        lease.close()
    recover_worker_state(config.DB_PATH)
    pool = RunnerPool(limit=vendor_limit)
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(
            poll_loop(config.DB_PATH, stop_event=stop), name="poller"
        ),
        asyncio.create_task(
            lease_heartbeat_loop(config.DB_PATH, process_id, stop_event=stop),
            name="process-lease",
        ),
    ]
    diagnostic_cleanup = _diagnostic_cleanup_coro(stop)
    if diagnostic_cleanup is not None:
        tasks.append(
            asyncio.create_task(
                diagnostic_cleanup, name="diagnostic-retention"
            )
        )
    if config.RETENTION_DAYS > 0:
        from server.retention import cleanup_loop

        tasks.append(
            asyncio.create_task(
                cleanup_loop(
                    config.DB_PATH,
                    retention_days=config.RETENTION_DAYS,
                    raw_dir=config.RAW_DIR,
                    stop_event=stop,
                ),
                name="retention",
            )
        )
    for index in range(min(2, vendor_limit)):
        tasks.append(
            asyncio.create_task(
                worker_loop(
                    config.DB_PATH,
                    worker_id=f"w{index + 1}",
                    owner_process_id=process_id,
                    stop_event=stop,
                    pool=pool,
                    recover=False,
                    wiki_enabled=index == 0,
                ),
                name=f"worker-w{index + 1}",
            )
        )
    app.state.background_tasks = tasks
    app.state.runtime_concurrency_limit = vendor_limit
    app.state.runtime_worker_lanes = min(2, vendor_limit)
    try:
        yield
    finally:
        results = await _shutdown_background_tasks(tasks, stop)
        for r in results:
            if isinstance(r, Exception):
                print(f"[lifespan] background loop exited with error: {r!r}")
        lease = connect(config.DB_PATH)
        try:
            process_repo.release(lease, process_id)
        finally:
            lease.close()


app = FastAPI(title="Almighty PR Review Server", lifespan=lifespan)
app.middleware("http")(protect_management_api)
app.include_router(harness_router)


async def _bounded_webhook_body(request: Request) -> bytes:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(400, "invalid content length") from exc
        if content_length < 0:
            raise HTTPException(400, "invalid content length")
        if content_length > config.WEBHOOK_MAX_BODY_BYTES:
            raise HTTPException(413, "webhook body too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > config.WEBHOOK_MAX_BODY_BYTES:
            raise HTTPException(413, "webhook body too large")
        body.extend(chunk)
    return bytes(body)


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
    return {"status": "ok", "admin_auth_required": bool(config.ADMIN_TOKEN)}


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


def _policy_snapshot_payload(snapshot) -> dict:
    if snapshot is None:
        return {
            "snapshot_status": "unknown",
            "scope": None,
            "dedupe": None,
            "cohort_key": None,
            "decision_hash": None,
            "config_hash": None,
            "benchmark_attestation_hash": None,
        }
    return {
        "snapshot_status": "known",
        "scope": {
            "requested_mode": snapshot.scope.requested_mode,
            "effective_mode": snapshot.scope.effective_mode,
            "reason": snapshot.scope.reason,
            "selection_source": snapshot.scope.selection_source,
        },
        "dedupe": {
            "requested_mode": snapshot.dedupe.requested_mode,
            "effective_mode": snapshot.dedupe.effective_mode,
            "reason": snapshot.dedupe.reason,
            "selection_source": snapshot.dedupe.selection_source,
        },
        "cohort_key": snapshot.cohort_key,
        "decision_hash": snapshot.decision_hash,
        "config_hash": snapshot.config_hash,
        "benchmark_attestation_hash": snapshot.benchmark_attestation_hash,
    }


def _repo_payload(row) -> dict:
    payload = dict(row)
    snapshot = resolve_policy_snapshot(row)
    payload["policy_decision"] = _policy_snapshot_payload(snapshot)
    for policy, decision in (("scope", snapshot.scope), ("dedupe", snapshot.dedupe)):
        payload[f"requested_review_{policy}_mode"] = decision.requested_mode
        payload[f"effective_review_{policy}_mode"] = decision.effective_mode
        payload[f"effective_review_{policy}_reason"] = decision.reason
        payload[f"review_{policy}_selection_source"] = decision.selection_source
    return payload


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
    try:
        rid = repo_repo.add(
            conn, full_name=full_name, local_path=body.local_path or None
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "이미 등록된 레포입니다.")
    return _repo_payload(repo_repo.get(conn, rid))


@app.get("/api/repos")
def list_repos(conn=Depends(get_conn)):
    rows = conn.execute(
        """SELECT r.*,
                  (SELECT COUNT(*) FROM pull_request p
                   WHERE p.repo_id=r.id AND p.state='open') AS open_pr_count
           FROM repo r ORDER BY r.full_name COLLATE NOCASE"""
    ).fetchall()
    return [_repo_payload(row) for row in rows]


@app.get("/api/overview")
def overview(conn=Depends(get_conn)):
    rows = conn.execute("""
      SELECT p.id, p.number, p.title, r.full_name AS repo,
             p.url, p.head_ref, p.head_sha, p.body,
             p.author, p.created_at, p.first_seen_at, p.is_draft,
             (SELECT complexity FROM pre_screen ps
                WHERE ps.pr_id=p.id AND ps.head_sha=p.head_sha
                ORDER BY ps.id DESC LIMIT 1) AS prescreen,
             (SELECT duration_ms FROM pre_screen ps
                WHERE ps.pr_id=p.id AND ps.head_sha=p.head_sha
                ORDER BY ps.id DESC LIMIT 1) AS prescreen_duration_ms,
             (SELECT COUNT(*) FROM finding f
                WHERE f.run_id=(SELECT id FROM review_run rr
                  WHERE rr.pr_id=p.id ORDER BY id DESC LIMIT 1)) AS finding_count,
             (SELECT MIN(CASE f.severity WHEN 'critical' THEN 0 WHEN 'high'
                THEN 1 WHEN 'medium' THEN 2 ELSE 3 END)
                FROM finding f
                WHERE f.run_id=(SELECT id FROM review_run rr
                  WHERE rr.pr_id=p.id ORDER BY id DESC LIMIT 1)) AS sev_rank,
             (SELECT id FROM review_run rr WHERE rr.pr_id=p.id
                ORDER BY id DESC LIMIT 1) AS run_id,
             (SELECT head_sha FROM review_run rr WHERE rr.pr_id=p.id
                ORDER BY id DESC LIMIT 1) AS run_head_sha,
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
                ORDER BY id DESC LIMIT 1) AS run_error,
             (SELECT status FROM review_job j
                WHERE j.pr_id=p.id AND j.head_sha=p.head_sha
                ORDER BY id DESC LIMIT 1) AS job_status,
             (SELECT error FROM review_job j
                WHERE j.pr_id=p.id AND j.head_sha=p.head_sha
                ORDER BY id DESC LIMIT 1) AS job_error,
             (SELECT next_run_at FROM review_job j
                WHERE j.pr_id=p.id AND j.head_sha=p.head_sha
                ORDER BY id DESC LIMIT 1) AS job_next_run_at
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


@app.get("/api/wiki")
def wiki(conn=Depends(get_conn)):
    """레포별 최신 Ground Truth Wiki 스냅샷과 생성 상태."""
    return wiki_repo.list_pages(conn)


@app.post("/api/repos/{rid}/wiki/refresh", status_code=202)
def refresh_wiki(rid: int, conn=Depends(get_conn)):
    repo = repo_repo.get(conn, rid)
    if repo is None:
        raise HTTPException(status_code=404, detail="repo not found")
    if not wiki_repo.mark_generating(conn, rid):
        raise HTTPException(status_code=409, detail="Wiki 생성이 이미 진행 중입니다")
    return wiki_repo.get_page(conn, rid)


@app.get("/api/learn")
def learn(conn=Depends(get_conn)):
    """레포별 사람 판단·Slack 반응·승인 가능한 리뷰 규칙을 반환한다."""
    repos = conn.execute("SELECT id, full_name FROM repo ORDER BY full_name").fetchall()
    out = []
    for r in repos:
        stats = feedback_source.repo_feedback_stats(conn, r["full_name"])
        slack = feedback_source.slack_counts(conn, r["full_name"])
        rules = review_rule_repo.list_for_repo(conn, r["id"])
        if (
            not stats["total"]
            and not (slack["positive"] or slack["negative"])
            and not rules
        ):
            continue
        out.append(
            {
                "repo_id": r["id"],
                "repo": r["full_name"],
                **stats,
                "slack_reactions": slack,
                "recent_decisions": feedback_source.recent_decisions(
                    conn, r["full_name"]
                ),
                "review_rules": rules,
            }
        )
    out.sort(key=lambda x: (-x["total"], x["repo"]))
    return out


@app.post("/api/repos/{rid}/review-rules/propose")
def propose_review_rules(rid: int, conn=Depends(get_conn)):
    repo = repo_repo.get(conn, rid)
    if repo is None:
        raise HTTPException(404, "repo not found")
    stats = feedback_source.repo_feedback_stats(conn, repo["full_name"])
    return review_rule_repo.propose_rules(conn, rid, stats["categories"])


class ReviewRulePatch(BaseModel):
    status: str


@app.patch("/api/review-rules/{rule_id}")
def patch_review_rule(rule_id: int, body: ReviewRulePatch, conn=Depends(get_conn)):
    try:
        rule = review_rule_repo.set_status(conn, rule_id, body.status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if rule is None:
        raise HTTPException(404, "review rule not found")
    return rule


class RepoPatch(BaseModel):
    full_name: str | None = None
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
    context_current_pr_reviews_on: int | None = None
    verify_singles_on: int | None = None
    incremental_review_on: int | None = None
    skip_draft_on: int | None = None
    static_context_path: str | None = None
    jira_project_keys: str | None = None
    db_schema_path: str | None = None
    live_db_target_id: str | None = None
    graphify_path: str | None = None
    review_scope_guard_mode: str | None = None
    review_dedupe_mode: str | None = None


@app.patch("/api/repos/{rid}")
def patch_repo(rid: int, body: RepoPatch, conn=Depends(get_conn)):
    current = repo_repo.get(conn, rid)
    if current is None:
        raise HTTPException(404, "repo not found")
    fields = body.model_dump(exclude_none=True)
    if body.full_name is not None:
        try:
            fields["full_name"] = _normalize_full_name(body.full_name)
        except ValueError:
            raise HTTPException(400, "owner/repo 형식으로 입력하세요.")
        duplicate = repo_repo.get_by_full_name(conn, fields["full_name"])
        if duplicate is not None and duplicate["id"] != rid:
            raise HTTPException(409, "이미 등록된 레포입니다.")
        if (
            fields["full_name"].casefold() != current["full_name"].casefold()
            and repo_repo.has_active_work(conn, rid)
        ):
            raise HTTPException(409, "진행 중인 작업을 완료하거나 취소한 뒤 이름을 변경하세요")
    if body.local_path is not None:
        fields["local_path"] = body.local_path.strip() or None
    if body.live_db_target_id is not None:
        from server.context.live_mssql_source import valid_target_id

        target_id = body.live_db_target_id.strip()
        if target_id and not valid_target_id(target_id):
            raise HTTPException(400, "live_db_target_id가 유효하지 않습니다")
        fields["live_db_target_id"] = target_id or None
    if body.trigger_mode is not None and body.trigger_mode not in ("auto", "manual"):
        raise HTTPException(400, "trigger_mode는 auto 또는 manual이어야 합니다")
    if body.claude_effort is not None and body.claude_effort not in config.CLAUDE_EFFORTS:
        raise HTTPException(
            400, f"claude_effort는 {'/'.join(config.CLAUDE_EFFORTS)} 중 하나여야 합니다"
        )
    if body.codex_effort is not None and body.codex_effort not in config.CODEX_EFFORTS:
        raise HTTPException(
            400, f"codex_effort는 {'/'.join(config.CODEX_EFFORTS)} 중 하나여야 합니다"
        )
    for key in ("review_scope_guard_mode", "review_dedupe_mode"):
        value = getattr(body, key)
        if value is None:
            continue
        normalized = value.strip().lower()
        if normalized not in {"", "observe", "enforce"}:
            raise HTTPException(400, f"{key}는 observe/enforce 또는 빈 값이어야 합니다")
        fields[key] = normalized or None
    binary_fields = (
        "enabled",
        "vendor_claude_on",
        "vendor_codex_on",
        "merge_enabled",
        "context_static_on",
        "context_jira_on",
        "context_db_schema_on",
        "context_graphify_on",
        "context_feedback_on",
        "context_current_pr_reviews_on",
        "verify_singles_on",
        "incremental_review_on",
        "skip_draft_on",
    )
    for key in binary_fields:
        value = getattr(body, key)
        if value is not None and value not in (0, 1):
            raise HTTPException(400, f"{key}는 0 또는 1이어야 합니다")
    if body.harness_name is not None:
        try:
            validate_harness_name(body.harness_name)
        except ValueError:
            raise HTTPException(400, "유효하지 않은 하네스 이름입니다")
        if body.harness_name not in list_harnesses():
            raise HTTPException(400, "존재하지 않는 하네스입니다")
    # None이 '상속(전역 기본값)으로 되돌림'을 뜻하는 필드는 exclude_none에서 되살린다.
    for key in (
        "context_static_on",
        "context_jira_on",
        "context_db_schema_on",
        "context_graphify_on",
        "context_feedback_on",
        "context_current_pr_reviews_on",
        "verify_singles_on",
        "incremental_review_on",
        "skip_draft_on",
        "claude_model",
        "claude_effort",
        "codex_model",
        "codex_effort",
    ):
        if key in body.model_fields_set and getattr(body, key) is None:
            fields[key] = None
    try:
        repo_repo.update(conn, rid, **fields)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "이미 등록된 레포입니다.")
    return _repo_payload(repo_repo.get(conn, rid))


@app.delete("/api/repos/{rid}")
def delete_repo(rid: int, conn=Depends(get_conn)):
    if repo_repo.get(conn, rid) is None:
        raise HTTPException(404, "repo not found")
    if repo_repo.has_active_work(conn, rid):
        raise HTTPException(409, "진행 중인 리뷰 또는 Wiki 생성을 먼저 완료하거나 취소하세요")
    repo_repo.remove(conn, rid)
    return {"deleted": rid}


def _settings_response(conn, request: Request) -> dict:
    out = dict(settings_repo.get(conn))
    applied = getattr(request.app.state, "runtime_concurrency_limit", None)
    out["runtime_concurrency_limit"] = applied
    out["runtime_worker_lanes"] = getattr(
        request.app.state, "runtime_worker_lanes", None
    )
    out["concurrency_restart_required"] = (
        None if applied is None else applied != out["concurrency_limit"]
    )
    return out


@app.get("/api/settings")
def get_settings(request: Request, conn=Depends(get_conn)):
    return _settings_response(conn, request)


@app.get("/api/settings/context-status")
def get_context_status(conn=Depends(get_conn)):
    from server.context.status import context_source_status

    return context_source_status(conn)


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
    context_current_pr_reviews_on: int | None = None
    verify_singles_on: int | None = None
    incremental_review_on: int | None = None
    skip_draft_on: int | None = None


@app.patch("/api/settings")
def patch_settings(
    body: SettingsPatch, request: Request, conn=Depends(get_conn)
):
    if body.concurrency_limit is not None and not (
        config.CONCURRENCY_MIN <= body.concurrency_limit <= config.CONCURRENCY_MAX
    ):
        raise HTTPException(
            400,
            f"concurrency_limit는 {config.CONCURRENCY_MIN} 이상 "
            f"{config.CONCURRENCY_MAX} 이하여야 합니다",
        )
    if body.default_poll_interval is not None and not (
        config.POLL_INTERVAL_MIN_SEC
        <= body.default_poll_interval
        <= config.POLL_INTERVAL_MAX_SEC
    ):
        raise HTTPException(
            400,
            "default_poll_interval은 "
            f"{config.POLL_INTERVAL_MIN_SEC}초 이상 "
            f"{config.POLL_INTERVAL_MAX_SEC}초 이하여야 합니다",
        )
    binary_fields = (
        "context_static_on",
        "context_jira_on",
        "context_db_schema_on",
        "context_graphify_on",
        "context_feedback_on",
        "context_current_pr_reviews_on",
        "verify_singles_on",
        "incremental_review_on",
        "skip_draft_on",
    )
    for key in binary_fields:
        value = getattr(body, key)
        if value is not None and value not in (0, 1):
            raise HTTPException(400, f"{key}는 0 또는 1이어야 합니다")
    common_efforts = set(config.CLAUDE_EFFORTS) & set(config.CODEX_EFFORTS)
    if body.default_effort is not None and body.default_effort not in common_efforts:
        raise HTTPException(
            400, "default_effort는 두 벤더가 모두 지원하는 effort여야 합니다"
        )
    if body.claude_effort is not None and body.claude_effort not in config.CLAUDE_EFFORTS:
        raise HTTPException(
            400, f"claude_effort는 {'/'.join(config.CLAUDE_EFFORTS)} 중 하나여야 합니다"
        )
    if body.codex_effort is not None and body.codex_effort not in config.CODEX_EFFORTS:
        raise HTTPException(
            400, f"codex_effort는 {'/'.join(config.CODEX_EFFORTS)} 중 하나여야 합니다"
        )
    if body.prescreen_model is not None and not is_valid_prescreen_model(
        body.prescreen_model
    ):
        raise HTTPException(400, "사전 스크리닝에는 Claude 모델만 사용할 수 있습니다")
    # threshold는 decide()가 KeyError로 죽는 유일한 자유입력 — 경계에서 거른다
    # (수락하면 이후 모든 리뷰가 prescreen 직후 실패하는 브릭 상태가 된다).
    if (
        body.prescreen_gate_threshold is not None
        and body.prescreen_gate_threshold not in THRESHOLDS
    ):
        raise HTTPException(
            400, f"prescreen_gate_threshold는 {'/'.join(THRESHOLDS)} 중 하나여야 합니다"
        )
    settings_repo.update(conn, **body.model_dump(exclude_none=True))
    return _settings_response(conn, request)


@app.get("/api/prs/{pid}/runs")
def pr_runs(pid: int, conn=Depends(get_conn)):
    """PR의 리뷰 run 이력(최신 먼저) — 재리뷰 후에도 과거 run의 결과를 찾아볼 수 있게."""
    if pr_repo.get(conn, pid) is None:
        raise HTTPException(404, "pr not found")
    rows = conn.execute(
        """SELECT r.id, r.head_sha, r.trigger, r.status, r.error,
                  r.started_at, r.finished_at,
                  r.scope_requested_mode, r.scope_effective_mode,
                  r.scope_policy_reason, r.scope_selection_source,
                  r.dedupe_requested_mode, r.dedupe_effective_mode,
                  r.dedupe_policy_reason, r.dedupe_selection_source,
                  r.policy_cohort_key, r.policy_decision_hash,
                  r.policy_config_hash, r.benchmark_attestation_hash,
                  (SELECT COUNT(*) FROM finding f WHERE f.run_id = r.id)
                    AS finding_count
           FROM review_run r WHERE r.pr_id=? ORDER BY r.id DESC""",
        (pid,),
    ).fetchall()
    response = []
    for row in rows:
        item = dict(row)
        item["policy_snapshot"] = _policy_snapshot_payload(
            policy_snapshot_from_row(row)
        )
        for key in (
            "scope_requested_mode", "scope_effective_mode", "scope_policy_reason",
            "scope_selection_source", "dedupe_requested_mode",
            "dedupe_effective_mode", "dedupe_policy_reason",
            "dedupe_selection_source", "policy_cohort_key",
            "policy_decision_hash", "policy_config_hash",
            "benchmark_attestation_hash",
        ):
            item.pop(key, None)
        response.append(item)
    return response


@app.get("/api/runs/{run_id}/findings")
def run_findings(run_id: int, conn=Depends(get_conn)):
    if review_repo.get_run(conn, run_id) is None:
        raise HTTPException(404, "run not found")
    return [dict(f) for f in finding_repo.list_for_run(conn, run_id)]


@app.get("/api/runs/{run_id}/vendor-results")
def run_vendor_results(run_id: int, conn=Depends(get_conn)):
    if review_repo.get_run(conn, run_id) is None:
        raise HTTPException(404, "run not found")
    # ★개정 (codex v6 [MEDIUM]): 실패 벤더 노출용. 프론트 ReviewSection이
    # status='failed' 벤더에 배지를 띄워 부분 실패를 사용자에게 알린다.
    return [dict(v) for v in review_repo.list_vendor_results(conn, run_id)]


@app.get("/api/vendor-results/{vr_id}/raw")
def vendor_result_raw(vr_id: int, conn=Depends(get_conn)):
    """Raw model transcripts are disabled by default and never served normally."""
    raise HTTPException(404, "원문 진단 비활성")


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
    if pr["state"] != "open":
        raise HTTPException(409, "닫힌 PR은 재시도할 수 없습니다")
    if pr["head_sha"] != run["head_sha"]:
        raise HTTPException(409, "PR가 갱신됨 — 전체 재리뷰를 사용하세요")
    if not _is_latest_run(conn, run):
        raise HTTPException(409, "과거 리뷰 run은 재시도할 수 없습니다")
    if run["status"] != "done":
        raise HTTPException(
            409, "부분 실패한 리뷰에만 사용 가능 — 전체 재리뷰를 사용하세요"
        )
    repo = repo_repo.get(conn, pr["repo_id"])
    if repo is None:
        raise HTTPException(404, "repo not found")
    if not repo["enabled"]:
        raise HTTPException(409, "비활성화된 레포에서는 재시도할 수 없습니다")
    enabled = {"claude"} if repo["vendor_claude_on"] else set()
    if repo["vendor_codex_on"]:
        enabled.add("codex")
    failed = set(review_repo.failed_vendors(conn, run_id))
    if not failed:
        raise HTTPException(409, "재시도할 실패 벤더가 없습니다")
    if failed - enabled:
        raise HTTPException(409, "실패 벤더를 모두 활성화한 뒤 재시도하세요")
    try:
        job_id = job_repo.enqueue_retry(
            conn, pr_id=run["pr_id"], head_sha=run["head_sha"], run_id=run_id
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
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
    # 최신-run 확인과 상태 변경을 한 write transaction에서 수행한다. 둘 사이에 worker가
    # 새 run을 INSERT하면 과거 finding을 변경할 수 있던 TOCTOU를 차단한다.
    conn.execute("BEGIN IMMEDIATE")
    try:
        finding = finding_repo.get(conn, fid)
        if finding is None:
            raise HTTPException(404, "finding not found")
        if finding["posting_operation_id"] is not None:
            raise HTTPException(409, "GitHub 포스팅 중인 finding은 변경할 수 없습니다")
        latest = conn.execute(
            """SELECT rr.id=(SELECT id FROM review_run latest
                               WHERE latest.pr_id=rr.pr_id ORDER BY id DESC LIMIT 1) AS is_latest,
                      rr.head_sha=(SELECT head_sha FROM pull_request p
                                   WHERE p.id=rr.pr_id) AS is_current_head
               FROM review_run rr WHERE rr.id=?""",
            (finding["run_id"],),
        ).fetchone()
        if latest is None or not latest["is_latest"] or not latest["is_current_head"]:
            raise HTTPException(409, "과거 리뷰 finding은 변경할 수 없습니다")
        if body.status not in {"approved", "dismissed", "edited"}:
            raise HTTPException(400, "status는 approved, dismissed, edited 중 하나여야 합니다")
        if body.status == "edited":
            edited_text = (body.edited_text or "").strip()
            if not edited_text:
                raise HTTPException(400, "edited 상태에는 비어 있지 않은 edited_text가 필요합니다")
            finding_repo.set_status(
                conn, fid, body.status, edited_text, commit=False
            )
        else:
            if body.edited_text is not None:
                raise HTTPException(400, "edited_text는 edited 상태에서만 변경할 수 있습니다")
            # status-only PATCH는 기존 edited_text를 보존한다.
            finding_repo.set_status(conn, fid, body.status, commit=False)
        result = dict(finding_repo.get(conn, fid))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


_gh = None


def get_gh():
    global _gh
    if _gh is None:
        _gh = GhClient()
    return _gh


_slack = None


def get_slack():
    """봇 토큰이 설정된 경우에만 Slack 클라이언트를 만든다(미설정=None → 게시 자동 비활성).
    토큰은 env-only이므로 여기서만 참조하고 sqlite엔 넣지 않는다."""
    global _slack
    if _slack is None and config.SLACK_BOT_TOKEN:
        from server.slack.client import SlackClient

        _slack = SlackClient(token=config.SLACK_BOT_TOKEN)
    return _slack


@app.get("/api/telemetry")
def telemetry(request: Request, conn=Depends(get_conn)):
    job_counts = {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM review_job GROUP BY status"
        ).fetchall()
    }
    vendor = [
        dict(row)
        for row in conn.execute(
            """SELECT vendor, status, COUNT(*) AS count,
                      CAST(AVG(duration_ms) AS INTEGER) AS avg_duration_ms
               FROM vendor_result
               WHERE started_at >= datetime('now', '-1 day')
               GROUP BY vendor, status"""
        ).fetchall()
    ]
    applied = getattr(request.app.state, "runtime_concurrency_limit", None)
    return {
        "schema_version": 1,
        "jobs": job_counts,
        "vendors_24h": vendor,
        "configured_concurrency": settings_repo.get(conn)["concurrency_limit"],
        "applied_concurrency": applied,
    }


@app.get("/api/health/deep")
def health_deep(
    request: Request, conn=Depends(get_conn), gh=Depends(get_gh)
) -> dict:
    """환경 점검과 background task/queue 상태를 함께 노출한다."""
    from server.health import deep_health

    out = deep_health(conn, gh)
    tasks = getattr(request.app.state, "background_tasks", None)
    if tasks is None:
        runtime = {"status": "unknown", "tasks": []}
    else:
        task_rows = [
            {
                "name": task.get_name(),
                "done": task.done(),
                "error": (
                    str(task.exception())[:300]
                    if task.done() and not task.cancelled() and task.exception()
                    else None
                ),
            }
            for task in tasks
        ]
        runtime = {
            "status": "degraded" if any(row["done"] for row in task_rows) else "healthy",
            "tasks": task_rows,
        }
        if runtime["status"] == "degraded":
            out["ok"] = False
    queue = conn.execute(
        """SELECT COUNT(*) AS queued,
                  CAST(MAX((julianday('now')-julianday(created_at))*86400) AS INTEGER)
                    AS oldest_age_seconds
           FROM review_job WHERE status='queued'"""
    ).fetchone()
    runtime["queue"] = dict(queue)
    out["runtime"] = runtime
    return out


@app.get("/api/repos/{rid}/readiness")
def repo_readiness(rid: int, conn=Depends(get_conn), gh=Depends(get_gh)) -> dict:
    from server.repo_readiness import check_repo_readiness

    repo = repo_repo.get(conn, rid)
    if repo is None:
        raise HTTPException(404, "repo not found")
    return check_repo_readiness(repo, gh)


def _enqueue_polled_pr(conn, pr_id: int) -> bool:
    """자동 폴링 job이 새로 생성·복구됐으면 True, 기존 job이면 False."""
    pr = pr_repo.get(conn, pr_id)
    _, changed = job_repo.enqueue_with_result(
        conn, pr_id=pr_id, head_sha=pr["head_sha"], trigger="auto"
    )
    return changed


def _sync_repo_now(conn, repo, gh) -> dict:
    from server.poller import sync_repo

    return sync_repo(
        conn,
        repo,
        list_prs=gh.list_open_prs,
        enqueue=lambda pr_id: _enqueue_polled_pr(conn, pr_id),
    )


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


@app.post("/api/repos/{rid}/sync")
def sync_registered_repo(rid: int, conn=Depends(get_conn), gh=Depends(get_gh)):
    repo = repo_repo.get(conn, rid)
    if repo is None:
        raise HTTPException(404, "repo not found")
    if not repo["enabled"]:
        raise HTTPException(409, "비활성 레포입니다. 활성화한 뒤 동기화하세요")
    try:
        return _sync_repo_now(conn, repo, gh)
    except GitHubCliError as exc:
        status = exc.http_status if exc.http_status in {401, 403, 404} else 502
        raise HTTPException(status, _github_error_message(exc))
    except Exception as exc:
        raise HTTPException(502, redact_secrets(f"{type(exc).__name__}: {exc}"))


@app.post("/api/repos/sync")
def sync_all_registered_repos(conn=Depends(get_conn), gh=Depends(get_gh)):
    from server.poller import poll_once

    results = poll_once(
        conn,
        list_prs=gh.list_open_prs,
        enqueue=lambda pr_id: _enqueue_polled_pr(conn, pr_id),
    )
    successful = [result for result in results if result["ok"]]
    return {
        "ok": all(result["ok"] for result in results),
        "results": results,
        "repositories": len(results),
        "open_prs": sum(result["open_prs"] for result in successful),
        "enqueued_jobs": sum(result["enqueued_jobs"] for result in successful),
    }


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
    if pr is None:
        raise HTTPException(404, "pr not found")
    repo = repo_repo.get(conn, pr["repo_id"])
    health = {
        "ok": False,
        "auth": {"ok": False, "login": None, "error": None},
        "repo": {"ok": False, "full_name": repo["full_name"], "error": None},
        "issue": {"ok": False, "number": pr["number"], "error": None},
        "message": "",
    }
    checks = [  # 순서 = 원인 좁히기 순(인증 → 레포 접근 → PR 존재), 첫 실패에서 중단
        ("auth", lambda: {"login": gh.preflight_user().get("login")}),
        (
            "repo",
            lambda: {
                "full_name": gh.preflight_repo(repo["full_name"]).get(
                    "full_name", repo["full_name"]
                )
            },
        ),
        (
            "issue",
            lambda: {
                "number": gh.preflight_issue(repo["full_name"], pr["number"]).get(
                    "number", pr["number"]
                )
            },
        ),
    ]
    for section, check in checks:
        try:
            health[section].update({"ok": True, **check()})
        except GitHubCliError as e:
            health[section].update(
                {"error": _github_error_message(e), "status": e.http_status}
            )
            health["message"] = health[section]["error"]
            return health
    health["ok"] = True
    health["message"] = "GitHub 포스팅 가능"
    return health


def _is_latest_run(conn, run) -> bool:
    latest = conn.execute(
        "SELECT id FROM review_run WHERE pr_id=? ORDER BY id DESC LIMIT 1",
        (run["pr_id"],),
    ).fetchone()
    return latest is not None and latest["id"] == run["id"]


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
    eligible = [
        finding for finding in rows
        if "posting_eligible" not in finding.keys() or finding["posting_eligible"]
    ]
    pending = [
        finding for finding in eligible
        if finding["status"] in POSTABLE_FINDING_STATUSES
    ]
    display = [
        finding for finding in eligible
        if finding["status"] in DISPLAYED_FINDING_STATUSES
    ]
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
    if review_repo.get_run(conn, run_id) is None:
        raise HTTPException(404, "run not found")
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


_TECH_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{2,}|\d{3,}")
_DUPLICATE_STOP_TOKENS = {
    "and", "api", "body", "code", "false", "for", "from", "http", "https",
    "into", "line", "main", "null", "path", "request", "response", "review",
    "service", "that", "the", "this", "true", "when", "with",
}


def _technical_tokens(text: str) -> set[str]:
    return {
        token.lower().rstrip(".")
        for token in _TECH_TOKEN_RE.findall(text or "")
        if token.lower().rstrip(".") not in _DUPLICATE_STOP_TOKENS
    }


def _looks_duplicate(finding, existing_body: str) -> bool:
    """자동 억제보다 precision을 우선한 게시 전 중복 후보 판정.

    자연어 전체 의미를 추측하지 않고, 정규화 claim 포함 또는 고유 기술 토큰 4개 이상이
    새 finding 쪽의 절반 이상 겹칠 때만 막는다. 사용자는 finding을 기각/수정 후 재시도한다.
    """
    claim = (finding["edited_text"] or finding["claim"] or "").strip().lower()
    normalized_body = " ".join((existing_body or "").lower().split())
    normalized_claim = " ".join(claim.split())
    if len(normalized_claim) >= 20 and normalized_claim in normalized_body:
        return True
    candidate = _technical_tokens(
        f"{finding['edited_text'] or finding['claim']} {finding['rationale'] or ''}"
    )
    existing = _technical_tokens(existing_body)
    if not candidate or not existing:
        return False
    shared = candidate & existing
    distinctive = any(re.search(r"[\d_./:-]", token) for token in shared)
    return (
        len(shared) >= 4
        and distinctive
        and len(shared) / len(candidate) >= 0.45
    )


def _github_duplicate_findings(conn, run_id: int, gh, repo, pr) -> tuple[list[dict], str]:
    context = gh.get_pr_review_context(repo["full_name"], pr["number"])
    remote_head = context.get("head_sha") or ""
    sources = (
        ("review", context.get("reviews") or []),
        ("inline", context.get("inline_comments") or []),
        ("conversation", context.get("conversation_comments") or []),
    )
    existing = []
    for kind, items in sources:
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("body"), str):
                continue
            if kind == "inline" and item.get("is_resolved") is True:
                continue
            body = item["body"]
            # 현재 Almighty review는 update 대상이지 외부 중복 근거가 아니다.
            if "<!-- almighty-review [" in body:
                continue
            existing.append((kind, item, body))

    duplicates = []
    for finding in finding_repo.list_for_run(conn, run_id):
        if finding["status"] not in POSTABLE_FINDING_STATUSES:
            continue
        for kind, item, body in existing:
            if not _looks_duplicate(finding, body):
                continue
            author = item.get("author") or item.get("user") or {}
            duplicates.append(
                {
                    "finding_id": finding["id"],
                    "claim": finding["edited_text"] or finding["claim"],
                    "source": kind,
                    "github_id": item.get("id"),
                    "author": author.get("login") if isinstance(author, dict) else None,
                }
            )
            break
    return duplicates, remote_head


def _assert_post_fresh(conn, gh, repo, pr, run) -> None:
    if not _is_latest_run(conn, run):
        raise HTTPException(409, "포스팅 도중 더 최신 리뷰 run이 생성되었습니다")
    current_pr = pr_repo.get(conn, pr["id"])
    if current_pr is None or current_pr["head_sha"] != run["head_sha"]:
        raise HTTPException(409, "포스팅 도중 PR head가 갱신되었습니다")
    get_head = getattr(gh, "get_pr_head", None)
    if callable(get_head):
        try:
            remote_head = get_head(repo["full_name"], pr["number"])
        except GitHubCliError as exc:
            raise HTTPException(502, _github_error_message(exc)) from exc
        if remote_head and remote_head != run["head_sha"]:
            raise HTTPException(409, "포스팅 도중 GitHub PR head가 갱신되었습니다")


def _post_to_slack(conn, slack, repo, pr, run_id):
    """게시된 리뷰를 Slack에 요약 게시하고 run↔메시지 매핑을 저장한다(👍/👎 반응 학습 신호 수집).
    슬랙 미설정/이미 게시한 run이면 스킵(멱등). 실패는 절대 게시 응답을 깨지 않는다(best-effort,
    B-INV-4/8과 동일 원칙). GitHub 게시가 모두 성공한 뒤(loop 이후)에만 호출된다."""
    if slack is None or not config.SLACK_CHANNEL:
        return
    if feedback_repo.has_slack_post(conn, run_id):
        return
    try:
        from server.slack.format import build_slack_message

        shown = [
            f
            for f in finding_repo.list_for_run(conn, run_id)
            if f["status"] in DISPLAYED_FINDING_STATUSES
        ]
        if not shown:
            return
        res = slack.post_message(
            channel=config.SLACK_CHANNEL,
            text=build_slack_message(
                full_name=repo["full_name"],
                number=pr["number"],
                url=pr["url"] or "",
                findings=shown,
            ),
        )
        if res.get("channel") and res.get("ts"):
            feedback_repo.record_slack_post(
                conn, run_id=run_id, channel=res["channel"], ts=res["ts"]
            )
    except Exception as e:  # never break posting — 토큰 유출 방지 위해 로그도 redact
        msg = str(e)
        if config.SLACK_BOT_TOKEN:
            msg = msg.replace(config.SLACK_BOT_TOKEN, "[redacted]")
        print(f"[slack] post skipped: {msg}")


@app.post("/api/runs/{run_id}/post")
def post_run(
    run_id: int, conn=Depends(get_conn), gh=Depends(get_gh), slack=Depends(get_slack)
):
    # 로컬 단일 프로세스 서버에서 같은 run의 동시 POST가 review를 두 번 create하는
    # 것을 막는다. 네트워크 호출 동안 SQLite write lock은 잡지 않는다.
    lock = _post_lock(run_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "같은 run의 포스팅이 이미 진행 중입니다")
    try:
        return _post_run_locked(run_id, conn=conn, gh=gh, slack=slack)
    finally:
        lock.release()


def _post_run_locked(run_id: int, *, conn, gh, slack):
    run = review_repo.get_run(conn, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    pr = pr_repo.get(conn, run["pr_id"])
    if not _is_latest_run(conn, run):
        raise HTTPException(409, "과거 리뷰 run은 게시할 수 없습니다")
    # 구 head의 run을 게시하면 라인 앵커가 어긋나고, PR 단위 update-or-create가
    # 최신 게시 리뷰 본문을 구 내용으로 덮는다 — retry_vendors와 동일한 신선도 가드.
    if pr["head_sha"] != run["head_sha"]:
        raise HTTPException(
            409,
            "PR head가 갱신되어 이 run은 게시할 수 없습니다. 최신 리뷰를 실행하세요.",
        )
    repo = repo_repo.get(conn, pr["repo_id"])
    health = _post_health(conn, pr["id"], gh)
    if not health["ok"]:
        return JSONResponse(
            status_code=_health_status_code(health),
            content={"detail": health},
        )
    try:
        duplicates, remote_head = _github_duplicate_findings(
            conn, run_id, gh, repo, pr
        )
    except Exception as exc:
        # fresh scan 없이 게시하면 옵션이 꺼진 PR에서 같은 지적을 다시 쓸 수 있다.
        message = redact_secrets(f"{type(exc).__name__}: {exc}")
        return JSONResponse(
            status_code=502,
            content={"detail": {"message": f"기존 GitHub 리뷰 중복 검사 실패: {message}"}},
        )
    if remote_head and remote_head != run["head_sha"]:
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "message": "GitHub의 PR head가 갱신되어 이 run은 게시할 수 없습니다. 동기화 후 최신 리뷰를 실행하세요.",
                    "run_head_sha": run["head_sha"],
                    "remote_head_sha": remote_head,
                }
            },
        )
    if duplicates:
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "message": "기존 GitHub 리뷰와 중복 가능성이 있는 finding이 있습니다. 기각하거나 수정한 뒤 다시 포스팅하세요.",
                    "duplicates": duplicates,
                }
            },
        )

    posted = []
    for comment in _comments_to_post(conn, run_id):
        _assert_post_fresh(conn, gh, repo, pr, run)
        vendor = comment["vendor"]
        try:
            operation = post_operation_repo.prepare(
                conn,
                run_id=run_id,
                vendor=vendor,
                body=comment["body"],
                all_ids=comment["all_ids"],
                new_ids=comment["new_ids"],
            )
        except post_operation_repo.PostingConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        body = f"{operation['body']}\n\n{operation['marker']}"
        operation_owner = uuid.uuid4().hex
        if operation["status"] != "remote_applied" and not post_operation_repo.claim(
            conn, operation["id"], operation_owner
        ):
            return JSONResponse(
                status_code=409,
                content={"detail": {
                    "message": "같은 vendor의 포스팅 operation이 이미 진행 중입니다",
                    "operation_id": operation["id"],
                }},
            )
        prev = posted_repo.latest_for_pr_vendor(conn, pr_id=pr["id"], vendor=vendor)
        reuse = (
            prev is not None and prev["kind"] == "review" and prev["github_comment_id"]
        )
        try:
            if operation["status"] == "remote_applied":
                res = {
                    "id": operation["remote_review_id"],
                    "html_url": operation["remote_url"],
                }
            else:
                # create/update 성공 뒤 DB 기록 전에 죽었어도 exact marker로 원격 review를
                # 다시 찾아 adopt한다. 애매한 실패에서 새 review를 만들지 않는다.
                res = post_operation_repo.find_remote_review(
                    gh, repo["full_name"], pr["number"], operation["marker"]
                )
                if res is None and reuse:
                    try:
                        res = gh.update_review(
                            repo["full_name"], pr["number"],
                            prev["github_comment_id"], body,
                        )
                    except GitHubCliError as e:
                        if e.http_status != 404:
                            raise
                        res = _create_review(
                            gh, repo, pr, run, body, comment["findings"]
                        )
                elif res is None:
                    res = _create_review(
                        gh, repo, pr, run, body, comment["findings"]
                    )
                post_operation_repo.mark_remote(
                    conn, operation["id"], review_id=str(res["id"]),
                    url=res["html_url"], owner_token=operation_owner,
                )
        except GitHubCliError as e:
            post_operation_repo.mark_error(
                conn, operation["id"], str(e), owner_token=operation_owner
            )
            return JSONResponse(
                status_code=502,
                content={
                    "detail": {
                        "message": _github_error_message(e),
                        "posted": posted,
                        "operation_id": operation["id"],
                        "failed": {
                            "vendor": vendor,
                            "error": _github_error_message(e),
                            "command_kind": e.command_kind,
                        },
                    }
                },
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            if prev is not None:
                posted_repo.supersede(conn, prev["id"], commit=False)
            posted_repo.add(
                conn,
                run_id=run_id,
                vendor=vendor,
                github_comment_id=str(res["id"]),
                url=res["html_url"],
                marker=operation["marker"],
                body=body,
                head_sha=run["head_sha"],
                finding_ids=comment["all_ids"],
                kind="review",
                commit=False,
            )
            post_operation_repo.finalize(conn, operation["id"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        posted.append({"vendor": vendor, "url": res["html_url"]})
    if (
        posted
    ):  # GitHub 게시 성공분이 있을 때만 Slack 반응 수집용 요약 게시(best-effort)
        _post_to_slack(conn, slack, repo, pr, run_id)
    return {"posted": posted}


@app.post("/api/prs/{pid}/review", status_code=202)
def trigger_review(pid: int, conn=Depends(get_conn)):
    pr = pr_repo.get(conn, pid)
    if pr is None:
        raise HTTPException(404, "pr not found")
    if pr["state"] != "open":
        raise HTTPException(409, "닫힌 PR은 리뷰할 수 없습니다")
    repo = repo_repo.get(conn, pr["repo_id"])
    if repo is None:
        raise HTTPException(404, "repo not found")
    if not repo["enabled"]:
        raise HTTPException(409, "비활성화된 레포는 리뷰할 수 없습니다")
    if not (repo["vendor_claude_on"] or repo["vendor_codex_on"]):
        raise HTTPException(409, "활성 리뷰 vendor가 최소 하나 필요합니다")
    try:
        job_id = job_repo.enqueue_manual(conn, pr_id=pid, head_sha=pr["head_sha"])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": job_id}


@app.post("/api/prs/{pid}/cancel-review")
def cancel_review(pid: int, conn=Depends(get_conn)):
    """대기 중(재시도 backoff 포함) 리뷰 잡을 **전부** 취소 — 한 PR에 (pr, sha)별
    잡이 여럿 공존할 수 있다. 원자 UPDATE(status='queued' 가드)라 worker가 이미
    claim한(running) 잡은 덮지 않는다. running만 있으면 409(벤더 중단 불가)."""
    n = job_repo.cancel_queued(conn, pid, error="사용자가 취소")
    if n == 0:
        running = conn.execute(
            "SELECT 1 FROM review_job WHERE pr_id=? AND status='running'", (pid,)
        ).fetchone()
        if running:
            raise HTTPException(409, "이미 실행 중인 리뷰는 취소할 수 없습니다")
        raise HTTPException(404, "대기 중인 리뷰 잡이 없습니다")
    return {"canceled": n, "status": "canceled"}


@app.post("/api/webhooks/github")
async def github_webhook(request: Request, conn=Depends(get_conn)):
    """GitHub pull_request 웹훅 수신 → poller와 동일 로직으로 리뷰 job enqueue.
    HMAC 검증(env-only 시크릿) 후 auto 레포·벤더·needs_review 게이트를 그대로 적용한다."""
    from server.github.webhook import parse_pull_request_event, verify_signature

    if not config.GITHUB_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    signature = request.headers.get("X-Hub-Signature-256")
    if not signature:
        raise HTTPException(status_code=401, detail="invalid signature")
    body = await _bounded_webhook_body(request)
    if not verify_signature(
        config.GITHUB_WEBHOOK_SECRET, body, signature
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
        base_sha=info["base_sha"],
        url=info["url"],
        state=info["state"],
        head_ref=info["head_ref"],
        body=info["body"],
        is_draft=info["is_draft"],
    )
    has_vendor = repo["vendor_claude_on"] or repo["vendor_codex_on"]
    skip_draft = _effective(repo, settings_repo.get(conn), "skip_draft_on")
    if (
        has_vendor
        and not (skip_draft and info["is_draft"])
        and pr_repo.needs_review(conn, pid)
    ):
        job_repo.enqueue(conn, pr_id=pid, head_sha=info["head_sha"], trigger="auto")
        return {"status": "enqueued", "pr": info["number"]}
    return {"status": "skipped", "pr": info["number"]}


@app.post("/api/webhooks/slack")
async def slack_webhook(request: Request, conn=Depends(get_conn)):
    """Slack Events 수신 → 우리가 게시한 리뷰 메시지의 👍/👎를 학습 신호로 적재.
    HMAC(v0) 검증 후 url_verification 챌린지 응답, reaction_added/removed만 처리한다.
    우리가 게시하지 않은 메시지·관심 밖 이모지는 무시(200). finding.status로 포착 안 되는
    외부 신호를 feedback_signal에 현재-상태로 기록한다(서브프로젝트 C)."""
    from server.slack.webhook import (
        is_fresh,
        parse_event,
        verdict_for,
        verify_signature,
    )

    ts = request.headers.get("X-Slack-Request-Timestamp")
    if not config.SLACK_SIGNING_SECRET:
        raise HTTPException(
            status_code=503, detail="slack signing secret not configured"
        )
    signature = request.headers.get("X-Slack-Signature")
    if not ts or not signature:
        raise HTTPException(status_code=401, detail="invalid signature")
    body = await _bounded_webhook_body(request)
    if not verify_signature(
        config.SLACK_SIGNING_SECRET,
        ts,
        body,
        signature,
    ):
        raise HTTPException(status_code=401, detail="invalid signature")
    if not is_fresh(ts):  # 서명은 유효해도 오래된(>5분) 요청은 재생 공격 → 거부
        raise HTTPException(status_code=401, detail="stale timestamp")
    event = parse_event(body)
    if event is None:
        return {"status": "ignored"}
    if event["type"] == "url_verification":
        return {"challenge": event["challenge"]}
    verdict = verdict_for(event["reaction"])
    if verdict is None:
        return {"status": "ignored"}  # 관심 밖 이모지
    run_id = feedback_repo.run_for_message(
        conn, channel=event["channel"], ts=event["ts"]
    )
    if run_id is None:
        return {"status": "ignored"}  # 우리가 게시한 리뷰 메시지가 아님
    if event["action"] == "added":
        feedback_repo.add_reaction(
            conn,
            run_id=run_id,
            slack_user=event["user"],
            reaction=event["reaction"],
            verdict=verdict,
        )
    else:
        feedback_repo.remove_reaction(
            conn, run_id=run_id, slack_user=event["user"], reaction=event["reaction"]
        )
    return {"status": "recorded", "verdict": verdict}
