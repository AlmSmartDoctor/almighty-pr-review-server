from server.db import connect, init_schema

EXPECTED_TABLES = {
    "repo",
    "pull_request",
    "pre_screen",
    "review_run",
    "vendor_result",
    "finding",
    "posted_comment",
    "app_settings",
    "review_job",  # ★개정
}


def test_init_schema_creates_all_tables(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert EXPECTED_TABLES <= names


def test_app_settings_seeded_single_row(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    rows = conn.execute("SELECT * FROM app_settings").fetchall()
    assert len(rows) == 1
    assert rows[0]["concurrency_limit"] == 2
    assert rows[0]["review_model"] == "sonnet"
    assert rows[0]["prescreen_model"] == "haiku"
    assert rows[0]["codex_model"] == ""


def test_connect_enables_wal(tmp_path):  # ★개정: 동시성 안전
    conn = connect(tmp_path / "test.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_repo_has_local_path_and_job_columns(tmp_path):  # ★개정
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    repo_cols = {r[1] for r in conn.execute("PRAGMA table_info(repo)")}
    assert {"local_path", "last_polled_at", "last_poll_error"} <= repo_cols
    job_cols = {r[1] for r in conn.execute("PRAGMA table_info(review_job)")}
    assert {"status", "attempts", "locked_by", "next_run_at"} <= job_cols
    pr_cols = {r[1] for r in conn.execute("PRAGMA table_info(pull_request)")}
    assert "created_at" in pr_cols


def test_init_schema_migrates_repo_poll_error_column(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.execute(
        """CREATE TABLE repo (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          full_name TEXT NOT NULL UNIQUE,
          last_polled_at TEXT
        )"""
    )

    init_schema(conn)
    init_schema(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(repo)")}
    assert "last_poll_error" in columns


def test_init_schema_migrates_app_settings_review_model(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.execute(
        """CREATE TABLE app_settings (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          concurrency_limit INTEGER NOT NULL DEFAULT 2,
          prescreen_model TEXT NOT NULL DEFAULT 'claude-haiku'
        )"""
    )
    conn.execute("INSERT INTO app_settings (id) VALUES (1)")
    init_schema(conn)
    init_schema(conn)
    row = conn.execute("SELECT * FROM app_settings WHERE id=1").fetchone()
    assert row["review_model"] == "sonnet"
    assert row["codex_model"] == ""
    # 레거시 'claude-haiku'(유효하지 않은 별칭) → 'haiku'로 정규화
    assert row["prescreen_model"] == "haiku"


def test_init_schema_migrates_pull_request_created_at(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.execute(
        """CREATE TABLE pull_request (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repo_id INTEGER NOT NULL,
          number INTEGER NOT NULL,
          title TEXT, author TEXT, head_sha TEXT NOT NULL,
          base_ref TEXT, state TEXT NOT NULL DEFAULT 'open',
          url TEXT, last_reviewed_sha TEXT,
          first_seen_at TEXT, updated_at TEXT,
          UNIQUE(repo_id, number)
        )"""
    )
    init_schema(conn)
    init_schema(conn)
    pr_cols = {r[1] for r in conn.execute("PRAGMA table_info(pull_request)")}
    assert "created_at" in pr_cols
