import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS repo (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  full_name TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  trigger_mode TEXT NOT NULL DEFAULT 'auto',      -- auto|manual
  vendor_claude_on INTEGER NOT NULL DEFAULT 1,
  vendor_codex_on INTEGER NOT NULL DEFAULT 1,
  merge_enabled INTEGER NOT NULL DEFAULT 0,
  harness_name TEXT NOT NULL DEFAULT 'default',
  local_path TEXT,                                -- ★개정: 로컬 clone 경로(worktree 소스). 등록 시 검증
  last_polled_at TEXT,
  last_poll_error TEXT
);
CREATE TABLE IF NOT EXISTS pull_request (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER NOT NULL REFERENCES repo(id),
  number INTEGER NOT NULL,
  title TEXT, author TEXT, head_sha TEXT NOT NULL,
  base_ref TEXT, base_sha TEXT, state TEXT NOT NULL DEFAULT 'open',
  url TEXT, created_at TEXT, last_reviewed_sha TEXT,
  first_seen_at TEXT, updated_at TEXT,
  UNIQUE(repo_id, number)
);
CREATE TABLE IF NOT EXISTS pre_screen (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES pull_request(id),
  head_sha TEXT NOT NULL, model TEXT,
  complexity TEXT, score REAL, reason TEXT,
  duration_ms INTEGER, decided TEXT,               -- review|skip|manual
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS review_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES pull_request(id),
  head_sha TEXT NOT NULL, trigger TEXT, effort TEXT,
  merge_enabled INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',           -- queued|running|done|failed|canceled
  started_at TEXT, finished_at TEXT, error TEXT,
  owner_process_id TEXT,
  owner_job_id INTEGER,
  scope_requested_mode TEXT,
  scope_effective_mode TEXT,
  scope_policy_reason TEXT,
  scope_selection_source TEXT,
  dedupe_requested_mode TEXT,
  dedupe_effective_mode TEXT,
  dedupe_policy_reason TEXT,
  dedupe_selection_source TEXT,
  policy_cohort_key TEXT,
  policy_decision_hash TEXT,
  policy_config_hash TEXT,
  benchmark_attestation_hash TEXT
);
CREATE TABLE IF NOT EXISTS vendor_result (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES review_run(id),
  vendor TEXT NOT NULL,                            -- claude|codex
  status TEXT, duration_ms INTEGER,
  raw_path TEXT, error TEXT,
  execution_meta TEXT
);
CREATE TABLE IF NOT EXISTS finding (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES review_run(id),
  vendor_result_id INTEGER REFERENCES vendor_result(id),
  vendor TEXT NOT NULL, file TEXT, line INTEGER,
  severity TEXT, category TEXT, claim TEXT, rationale TEXT,
  confidence REAL, consensus TEXT DEFAULT 'single',
  consensus_group_id INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',          -- pending|approved|dismissed|edited|posted
  edited_text TEXT, created_at TEXT,
  posting_operation_id INTEGER,
  source_chunk_index INTEGER,
  owner_chunk_index INTEGER,
  scope_status TEXT,
  posting_eligible INTEGER NOT NULL DEFAULT 1,
  duplicate_group_id INTEGER,
  duplicate_suggested INTEGER NOT NULL DEFAULT 0
);
-- 서브프로젝트 C: finding 사람 판단 이력(append-only 감사). set_status가 상태 변경 시 1행 append.
CREATE TABLE IF NOT EXISTS finding_decision (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id INTEGER NOT NULL REFERENCES finding(id),
  from_status TEXT, to_status TEXT NOT NULL,
  decided_at TEXT NOT NULL
);
-- 사람이 승인한 레포별 리뷰 규칙. 제안은 자동 적용하지 않고 active 상태만 프롬프트에 주입한다.
CREATE TABLE IF NOT EXISTS review_rule (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER NOT NULL REFERENCES repo(id) ON DELETE CASCADE,
  category TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed',          -- proposed|active|disabled
  evidence_total INTEGER NOT NULL DEFAULT 0,
  evidence_rejected INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(repo_id, category)
);
CREATE TABLE IF NOT EXISTS posted_comment (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES review_run(id),
  vendor TEXT NOT NULL,                            -- claude|codex|merged
  github_comment_id TEXT, url TEXT, marker TEXT,
  body TEXT, posted_at TEXT,
  head_sha TEXT,                                  -- ★개정: update-or-create 판단 키
  finding_ids TEXT,                               -- ★개정: 포함 finding id(JSON)
  superseded_at TEXT                              -- ★개정: 재리뷰로 대체된 시점
);
-- 서브프로젝트 C(Slack 반응 루프): 리뷰를 게시한 Slack 메시지 매핑(run ↔ channel:ts).
-- 반응 웹훅이 (channel, ts)로 run을 역참조해 레포 스코프 학습 신호로 귀속시킨다.
CREATE TABLE IF NOT EXISTS slack_post (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES review_run(id),
  channel TEXT NOT NULL,
  ts TEXT NOT NULL,
  posted_at TEXT NOT NULL,
  UNIQUE(channel, ts)                             -- 한 메시지 = 한 매핑(멱등)
);
-- 서브프로젝트 C: finding.status로 포착 안 되는 외부 학습 신호(Slack 👍/👎). 현재-상태 모델
-- (reaction_added = INSERT OR IGNORE, reaction_removed = DELETE). run_id로 레포 스코프.
CREATE TABLE IF NOT EXISTS feedback_signal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES review_run(id),
  source TEXT NOT NULL,                            -- 'slack'
  slack_user TEXT NOT NULL,
  reaction TEXT NOT NULL,                          -- 원본 emoji 이름(colon 없이)
  verdict TEXT NOT NULL,                           -- positive|negative
  created_at TEXT NOT NULL,
  UNIQUE(run_id, source, slack_user, reaction)     -- 한 사람·한 이모지 = 한 신호
);
-- 레포 Ground Truth Wiki 최신 스냅샷. content/sources는 구조화 JSON이며 사람의 finding과
-- 분리한다. 레포 코드·문서·DB 스키마를 다시 읽어 refresh할 때 이 행을 원자적으로 교체한다.
CREATE TABLE IF NOT EXISTS wiki_page (
  repo_id INTEGER PRIMARY KEY REFERENCES repo(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'empty',            -- empty|generating|ready|failed
  content TEXT,
  sources TEXT,
  source_sha TEXT,
  generated_at TEXT,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  next_run_at TEXT,
  locked_by TEXT,
  locked_at TEXT,
  owner_process_id TEXT,
  updated_at TEXT NOT NULL
);
-- ★개정: 스케줄링 상태(review_job)와 실행 이력(review_run) 분리.
CREATE TABLE IF NOT EXISTS review_job (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES pull_request(id),
  head_sha TEXT NOT NULL,
  trigger TEXT NOT NULL DEFAULT 'auto',            -- auto|manual|retry
  status TEXT NOT NULL DEFAULT 'queued',           -- queued|running|done|failed|canceled
  priority INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  locked_by TEXT, locked_at TEXT,
  owner_process_id TEXT,
  next_run_at TEXT,                                -- backoff/rate-limit 지연
  run_id INTEGER REFERENCES review_run(id),
  retry_run_id INTEGER REFERENCES review_run(id),  -- trigger='retry'가 이어받을 대상 run
  error TEXT, created_at TEXT,
  UNIQUE(pr_id, head_sha)                          -- 같은 sha 중복 잡 방지(idempotency)
);
CREATE TABLE IF NOT EXISTS process_lease (
  process_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS github_post_operation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_key TEXT NOT NULL UNIQUE,
  run_id INTEGER NOT NULL REFERENCES review_run(id),
  vendor TEXT NOT NULL,
  marker TEXT NOT NULL UNIQUE,
  body TEXT NOT NULL,
  finding_ids TEXT NOT NULL,
  new_finding_ids TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  owner_token TEXT,
  locked_at TEXT,
  remote_review_id TEXT,
  remote_url TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS app_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  default_effort TEXT NOT NULL DEFAULT 'medium',
  concurrency_limit INTEGER NOT NULL DEFAULT 2,
  default_poll_interval INTEGER NOT NULL DEFAULT 60,
  prescreen_model TEXT NOT NULL DEFAULT 'haiku',
  review_model TEXT NOT NULL DEFAULT 'sonnet',
  codex_model TEXT NOT NULL DEFAULT '',
  prescreen_gate_threshold TEXT NOT NULL DEFAULT 'moderate'
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """커넥션 1개 = 1 사용 단위(요청/worker 잡). 전역 공유 금지(★개정).

    WAL + busy_timeout으로 reader/writer 동시성 및 잠깐의 락 경합을 흡수.
    """
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_column(conn, "repo", "last_poll_error", "TEXT")
    _ensure_column(conn, "pull_request", "created_at", "TEXT")
    _ensure_column(conn, "pull_request", "head_ref", "TEXT")
    _ensure_column(conn, "pull_request", "body", "TEXT")
    _ensure_column(conn, "pull_request", "base_sha", "TEXT")
    _ensure_column(conn, "pull_request", "is_draft", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "review_job", "retry_run_id", "INTEGER")
    _ensure_column(conn, "wiki_page", "locked_by", "TEXT")
    _ensure_column(conn, "wiki_page", "locked_at", "TEXT")
    _ensure_column(conn, "wiki_page", "attempts", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(
        conn, "wiki_page", "max_attempts", "INTEGER NOT NULL DEFAULT 3"
    )
    _ensure_column(conn, "wiki_page", "next_run_at", "TEXT")
    _ensure_column(
        conn, "app_settings", "review_model", "TEXT NOT NULL DEFAULT 'sonnet'"
    )
    _ensure_column(conn, "app_settings", "codex_model", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "review_run", "context_text", "TEXT")
    _ensure_column(conn, "review_run", "context_meta", "TEXT")
    _ensure_column(conn, "review_run", "context_chunks", "TEXT")
    # v2-B 프로바이더 토글 — 전역 기본(app_settings). NOT NULL DEFAULT 0.
    _ensure_column(
        conn, "app_settings", "context_static_on", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(
        conn, "app_settings", "context_jira_on", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(
        conn, "app_settings", "context_db_schema_on", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(
        conn, "app_settings", "context_graphify_on", "INTEGER NOT NULL DEFAULT 0"
    )
    # 자가 학습(팀 피드백) 토글 — finding의 사람 판단을 요약 주입. 별도 소스 컬럼 없음(앱 DB를 읽음).
    _ensure_column(
        conn, "app_settings", "context_feedback_on", "INTEGER NOT NULL DEFAULT 0"
    )
    # 현재 PR에 이미 남은 GitHub review/inline/conversation 댓글 참조 토글.
    _ensure_column(
        conn, "app_settings", "context_current_pr_reviews_on", "INTEGER NOT NULL DEFAULT 0"
    )
    # per-repo override — nullable(NULL이면 global 상속). 비밀 아님.
    _ensure_column(conn, "repo", "context_static_on", "INTEGER")
    _ensure_column(conn, "repo", "context_jira_on", "INTEGER")
    _ensure_column(conn, "repo", "context_db_schema_on", "INTEGER")
    _ensure_column(conn, "repo", "context_graphify_on", "INTEGER")
    _ensure_column(conn, "repo", "context_feedback_on", "INTEGER")
    _ensure_column(conn, "repo", "context_current_pr_reviews_on", "INTEGER")
    _ensure_column(conn, "repo", "static_context_path", "TEXT")
    _ensure_column(conn, "repo", "jira_project_keys", "TEXT")
    # DBSchema 정적 소스: 레포에 체크인된 DDL 덤프 경로(비밀 아님, root 하위 봉쇄).
    _ensure_column(conn, "repo", "db_schema_path", "TEXT")
    # Resolver-backed MSSQL Gateway scope ID(비밀 아님). DB 주소·자격증명은 env/Gateway에만 존재.
    _ensure_column(conn, "repo", "live_db_target_id", "TEXT")
    # Graphify 애그리게이터 1차 소스: 레포에 체크인된 프로젝트 문서 경로(root 하위 봉쇄).
    _ensure_column(conn, "repo", "graphify_path", "TEXT")
    # 레포별·벤더별 모델/effort — NULL/''이면 전역 기본(app_settings) 상속, 전역도 없으면 코드 기본값.
    # claude는 --model/--effort, codex는 --model/-c model_reasoning_effort로 각각 전달.
    _ensure_column(conn, "repo", "claude_model", "TEXT")
    _ensure_column(conn, "repo", "claude_effort", "TEXT")
    _ensure_column(conn, "repo", "codex_model", "TEXT")
    _ensure_column(conn, "repo", "codex_effort", "TEXT")
    # 전역 기본 effort의 벤더별 분리 — NULL이면 default_effort로 폴백(비파괴 상속).
    _ensure_column(conn, "app_settings", "claude_effort", "TEXT")
    _ensure_column(conn, "app_settings", "codex_effort", "TEXT")
    # 고위험 SINGLE finding 반박 패스 토글 — 전역 기본 + per-repo override(NULL=상속).
    _ensure_column(
        conn, "app_settings", "verify_singles_on", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(conn, "repo", "verify_singles_on", "INTEGER")
    # 반박 패스 결과(감사·트리아지) — finding당 verdict + 근거.
    _ensure_column(conn, "finding", "verify_status", "TEXT")
    _ensure_column(conn, "finding", "verify_rationale", "TEXT")
    _ensure_column(conn, "finding", "verify_independent", "INTEGER")
    _ensure_column(conn, "finding", "verify_evidence_status", "TEXT")
    # 청크 ownership/scope와 non-destructive duplicate shadow grouping.
    _ensure_column(conn, "finding", "source_chunk_index", "INTEGER")
    _ensure_column(conn, "finding", "owner_chunk_index", "INTEGER")
    _ensure_column(conn, "finding", "scope_status", "TEXT")
    _ensure_column(
        conn, "finding", "posting_eligible", "INTEGER NOT NULL DEFAULT 1"
    )
    _ensure_column(conn, "finding", "duplicate_group_id", "INTEGER")
    _ensure_column(
        conn, "finding", "duplicate_suggested", "INTEGER NOT NULL DEFAULT 0"
    )
    # 벤더 리뷰 시작 시각 — running 중 상세 트레이스의 실시간 경과시간 계산용.
    _ensure_column(conn, "vendor_result", "started_at", "TEXT")
    # 원문 transcript 없이 attempt/phase/chunk별 숫자·상태 telemetry만 저장.
    _ensure_column(conn, "vendor_result", "execution_meta", "TEXT")
    # 증분 리뷰 토글 — 전역 기본 ON + per-repo override(NULL=상속). 켜면 재리뷰가
    # 직전 완료(done) 런 이후의 델타만 리뷰. review_run.base_sha=델타 기준 sha(NULL=전체).
    _ensure_column(
        conn, "app_settings", "incremental_review_on", "INTEGER NOT NULL DEFAULT 1"
    )
    _ensure_column(conn, "repo", "incremental_review_on", "INTEGER")
    _ensure_column(conn, "review_run", "base_sha", "TEXT")
    # 포스팅 프리미티브 구분 — 'issue'(레거시 issue comment) | 'review'(PR review).
    # review 행은 github_comment_id에 review_id를 담고, 재게시 시 PUT로 본문만 갱신한다.
    _ensure_column(conn, "posted_comment", "kind", "TEXT NOT NULL DEFAULT 'issue'")
    # prescreen 결과 재사용 키 — diff 내용 해시(full/incremental 무관하게 정확).
    _ensure_column(conn, "pre_screen", "diff_hash", "TEXT")
    # draft PR 자동 리뷰 skip — 전역 기본 ON(draft=미완성 선언) + per-repo override(NULL=상속).
    # 수동 트리거는 게이트 밖(사람이 draft를 알고 누른 것).
    _ensure_column(conn, "app_settings", "skip_draft_on", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "repo", "skip_draft_on", "INTEGER")
    # repo별 scope/dedupe canary override(NULL=env global mode).
    _ensure_column(conn, "repo", "review_scope_guard_mode", "TEXT")
    _ensure_column(conn, "repo", "review_dedupe_mode", "TEXT")
    _ensure_column(conn, "review_job", "owner_process_id", "TEXT")
    _ensure_column(conn, "review_run", "owner_process_id", "TEXT")
    _ensure_column(conn, "review_run", "owner_job_id", "INTEGER")
    # 최초 실행 시점의 immutable policy decision. 레거시 row는 NULL=unknown이며
    # 현재 env/repo 설정으로 backfill하지 않는다.
    for column in (
        "scope_requested_mode", "scope_effective_mode", "scope_policy_reason",
        "scope_selection_source", "dedupe_requested_mode", "dedupe_effective_mode",
        "dedupe_policy_reason", "dedupe_selection_source", "policy_cohort_key",
        "policy_decision_hash", "policy_config_hash", "benchmark_attestation_hash",
    ):
        _ensure_column(conn, "review_run", column, "TEXT")
    _ensure_column(conn, "wiki_page", "owner_process_id", "TEXT")
    _ensure_column(conn, "finding", "posting_operation_id", "INTEGER")
    _ensure_column(conn, "github_post_operation", "owner_token", "TEXT")
    _ensure_column(conn, "github_post_operation", "locked_at", "TEXT")
    # 레거시 'claude-haiku'는 유효한 CLI 별칭이 아니다(옛 기본값·미사용 죽은 값).
    # 이제 사전 스크리닝이 이 값을 실제로 subprocess에 넘기므로 유효 별칭으로 정규화.
    conn.execute(
        "UPDATE app_settings SET prescreen_model='haiku' "
        "WHERE prescreen_model='claude-haiku'"
    )
    conn.execute("INSERT OR IGNORE INTO app_settings (id) VALUES (1)")
    # 과거 UI가 허용했던 Codex/타사 모델 값은 런타임마다 조용히 haiku로 폴백해
    # 설정 표시와 실제 실행이 달랐다. 시작 migration에서 실제 유효값으로 일치시킨다.
    from server.review.prescreen import is_valid_prescreen_model

    current = conn.execute(
        "SELECT prescreen_model FROM app_settings WHERE id=1"
    ).fetchone()[0]
    if not is_valid_prescreen_model(current):
        conn.execute("UPDATE app_settings SET prescreen_model='haiku' WHERE id=1")
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_review_job_claim
          ON review_job(status, next_run_at, priority DESC, id);
        CREATE INDEX IF NOT EXISTS idx_review_run_pr_status
          ON review_run(pr_id, status, id DESC);
        CREATE INDEX IF NOT EXISTS idx_pre_screen_reuse
          ON pre_screen(pr_id, diff_hash, model, id DESC);
        CREATE INDEX IF NOT EXISTS idx_finding_run_status
          ON finding(run_id, status, vendor, id);
        CREATE INDEX IF NOT EXISTS idx_pull_request_repo_open
          ON pull_request(repo_id, number) WHERE state='open';
        """
    )
    conn.commit()


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
