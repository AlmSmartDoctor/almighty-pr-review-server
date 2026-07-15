ALLOWED = {
    "default_effort",
    "concurrency_limit",
    "default_poll_interval",
    "prescreen_model",
    "review_model",
    "codex_model",
    "prescreen_gate_threshold",
    "context_static_on",
    "context_jira_on",
    "context_db_schema_on",
    "context_graphify_on",
    "context_feedback_on",
    "verify_singles_on",
    "incremental_review_on",
}


def get(conn):
    return conn.execute("SELECT * FROM app_settings WHERE id=1").fetchone()


def update(conn, **fields):
    cols = [c for c in fields if c in ALLOWED]
    if not cols:
        return
    sets = ", ".join(f"{c}=?" for c in cols)
    conn.execute(
        f"UPDATE app_settings SET {sets} WHERE id=1",
        [fields[c] for c in cols],
    )
    conn.commit()
