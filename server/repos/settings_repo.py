ALLOWED = {
    "default_effort",
    "concurrency_limit",
    "default_poll_interval",
    "approval_gate_on",
    "prescreen_model",
    "review_model",
    "codex_model",
    "prescreen_gate_threshold",
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
