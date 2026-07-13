import sqlite3


def add(conn: sqlite3.Connection, *, full_name: str, **overrides) -> int:
    cur = conn.execute("INSERT INTO repo (full_name) VALUES (?)", (full_name,))
    conn.commit()
    rid = cur.lastrowid
    if overrides:
        update(conn, rid, **overrides)
    return rid


def get(conn: sqlite3.Connection, rid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM repo WHERE id = ?", (rid,)).fetchone()


def get_by_full_name(conn: sqlite3.Connection, full_name: str) -> sqlite3.Row | None:
    # GitHub full_name은 대소문자 무시(정규 casing과 등록 casing이 달라도 매칭) → NOCASE.
    return conn.execute(
        "SELECT * FROM repo WHERE full_name = ? COLLATE NOCASE", (full_name,)
    ).fetchone()


def list_enabled(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM repo WHERE enabled = 1").fetchall()


ALLOWED = {
    "enabled",
    "trigger_mode",
    "poll_interval_sec",
    "default_effort",
    "vendor_claude_on",
    "vendor_codex_on",
    "merge_enabled",
    "auto_post",
    "harness_name",
    "local_path",
    "last_polled_at",  # ★개정: local_path
    "context_static_on",
    "context_jira_on",
    "context_db_schema_on",
    "context_graphify_on",
    "static_context_path",
    "jira_project_keys",
    "db_schema_path",
    "graphify_path",
    "verify_singles_on",
    "incremental_review_on",
}


def update(conn: sqlite3.Connection, rid: int, **fields) -> None:
    cols = [c for c in fields if c in ALLOWED]
    if not cols:
        return
    sets = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(
        f"UPDATE repo SET {sets} WHERE id = ?",
        [fields[c] for c in cols] + [rid],
    )
    conn.commit()
