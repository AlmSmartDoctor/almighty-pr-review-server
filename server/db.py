import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS repo (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  full_name TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  trigger_mode TEXT NOT NULL DEFAULT 'auto',      -- auto|manual
  poll_interval_sec INTEGER NOT NULL DEFAULT 60,
  default_effort TEXT NOT NULL DEFAULT 'medium',
  vendor_claude_on INTEGER NOT NULL DEFAULT 1,
  vendor_codex_on INTEGER NOT NULL DEFAULT 1,
  merge_enabled INTEGER NOT NULL DEFAULT 0,
  auto_post INTEGER NOT NULL DEFAULT 0,
  harness_name TEXT NOT NULL DEFAULT 'default',
  local_path TEXT,                                -- ★개정: 로컬 clone 경로(worktree 소스). 등록 시 검증
  last_polled_at TEXT
);
CREATE TABLE IF NOT EXISTS harness (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  scope TEXT NOT NULL DEFAULT 'global',            -- global|repo|situation
  path TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS pull_request (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER NOT NULL REFERENCES repo(id),
  number INTEGER NOT NULL,
  title TEXT, author TEXT, head_sha TEXT NOT NULL,
  base_ref TEXT, state TEXT NOT NULL DEFAULT 'open',
  url TEXT, last_reviewed_sha TEXT,
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
  started_at TEXT, finished_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS vendor_result (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES review_run(id),
  vendor TEXT NOT NULL,                            -- claude|codex
  status TEXT, duration_ms INTEGER, tokens INTEGER,
  raw_path TEXT, error TEXT
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
  edited_text TEXT, created_at TEXT
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
-- ★개정: 스케줄링 상태(review_job)와 실행 이력(review_run) 분리.
CREATE TABLE IF NOT EXISTS review_job (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES pull_request(id),
  head_sha TEXT NOT NULL,
  trigger TEXT NOT NULL DEFAULT 'auto',            -- auto|manual
  status TEXT NOT NULL DEFAULT 'queued',           -- queued|running|done|failed|canceled
  priority INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  locked_by TEXT, locked_at TEXT,
  next_run_at TEXT,                                -- backoff/rate-limit 지연
  run_id INTEGER REFERENCES review_run(id),
  error TEXT, created_at TEXT,
  UNIQUE(pr_id, head_sha)                          -- 같은 sha 중복 잡 방지(idempotency)
);
CREATE TABLE IF NOT EXISTS app_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  default_effort TEXT NOT NULL DEFAULT 'medium',
  concurrency_limit INTEGER NOT NULL DEFAULT 2,
  default_poll_interval INTEGER NOT NULL DEFAULT 60,
  approval_gate_on INTEGER NOT NULL DEFAULT 1,
  prescreen_model TEXT NOT NULL DEFAULT 'claude-haiku',
  prescreen_gate_threshold TEXT NOT NULL DEFAULT 'moderate'
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """커넥션 1개 = 1 사용 단위(요청/worker 잡). 전역 공유 금지(★개정).

    WAL + busy_timeout으로 reader/writer 동시성 및 잠깐의 락 경합을 흡수.
    """
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO app_settings (id) VALUES (1)")
    conn.execute(
        "INSERT OR IGNORE INTO harness (name, scope, path) VALUES "
        "('default', 'global', 'harness/default')"
    )
    conn.commit()
