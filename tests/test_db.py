from server.db import connect, init_schema

EXPECTED_TABLES = {
    "repo",
    "harness",
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


def test_connect_enables_wal(tmp_path):  # ★개정: 동시성 안전
    conn = connect(tmp_path / "test.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_repo_has_local_path_and_job_columns(tmp_path):  # ★개정
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    repo_cols = {r[1] for r in conn.execute("PRAGMA table_info(repo)")}
    assert "local_path" in repo_cols
    job_cols = {r[1] for r in conn.execute("PRAGMA table_info(review_job)")}
    assert {"status", "attempts", "locked_by", "next_run_at"} <= job_cols
