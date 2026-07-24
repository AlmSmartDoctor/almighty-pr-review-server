"""Bounded, privacy-safe queries for the review system operations dashboard."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any

HORIZONS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
MAX_RUNS = 5_000
MAX_ACTIVE_JOBS = 50
MAX_FAILURES = 20
_BATCH_SIZE = 500
_RUN_STATUSES = frozenset({"queued", "running", "done", "failed", "canceled"})
_VENDOR_FAILURES = frozenset({"failed", "partial", "timeout", "canceled"})


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def normalize_filters(*, repo_id: int | None, horizon: str) -> dict[str, Any]:
    if repo_id is not None and repo_id < 1:
        raise ValueError("repo_id must be positive")
    if horizon not in HORIZONS:
        raise ValueError("range must be one of 24h, 7d, 30d")
    return {"repo_id": repo_id, "range": horizon}


def window(filters: dict[str, Any], *, as_of: datetime | None = None) -> tuple[str, str]:
    end = as_of or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return _timestamp(end), _timestamp(end - HORIZONS[filters["range"]])


def failure_code(value: Any) -> str:
    """Classify an error without returning paths, commands, prompts, or raw output."""
    text = value.lower() if isinstance(value, str) else ""
    checks = (
        ("output_limit", ("output_limit", "output limit", "too large")),
        ("timeout", ("timeout", "timed out", "deadline")),
        ("authentication", ("auth", "credential", "login", "token")),
        ("cleanup", ("cleanup", "terminate", "process group")),
        ("runtime_setup", ("runtime_setup", "setup failed", "materialization")),
        ("canceled", ("cancel", "stale head", "closed pr")),
        ("rate_limit", ("rate limit", "429")),
    )
    for code, needles in checks:
        if any(needle in text for needle in needles):
            return code
    return "unknown"


def duration_ms(
    started_at: str | None, finished_at: str | None, *, running: bool = False
) -> int | None:
    if not started_at:
        return None
    if not finished_at and running:
        finished_at = _timestamp(datetime.now(timezone.utc))
    if not finished_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        finish = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    value = int((finish - start).total_seconds() * 1000)
    return value if value >= 0 else None


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, ceil(percentile * len(ordered)) - 1)]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _batched(values: list[int]):
    for offset in range(0, len(values), _BATCH_SIZE):
        yield values[offset:offset + _BATCH_SIZE]


def select_runs(
    conn, filters: dict[str, Any], *, start: str, end: str
) -> tuple[list[dict[str, Any]], bool]:
    repo_id = filters["repo_id"]
    index = "idx_review_run_dashboard_repo" if repo_id is not None else "idx_review_run_dashboard_global"
    where = ["rr.started_at IS NOT NULL", "rr.started_at>=?", "rr.started_at<=?"]
    params: list[Any] = [start, end]
    if repo_id is not None:
        where.append("rr.repo_id=?")
        params.append(repo_id)
    rows = conn.execute(
        f"""WITH candidates AS MATERIALIZED (
               SELECT rr.id, rr.pr_id, rr.repo_id, rr.status, rr.trigger,
                      rr.started_at, rr.finished_at, rr.error
               FROM review_run rr INDEXED BY {index}
               WHERE {' AND '.join(where)}
               ORDER BY rr.started_at DESC, rr.id DESC
               LIMIT ?
             )
             SELECT c.id, c.pr_id, c.repo_id, c.status, c.trigger,
                    c.started_at, c.finished_at, c.error,
                    p.number AS pr_number, p.title AS pr_title,
                    r.full_name AS repo_name
             FROM candidates c
             JOIN pull_request p ON p.id=c.pr_id
             JOIN repo r ON r.id=p.repo_id
             ORDER BY c.started_at DESC, c.id DESC""",
        (*params, MAX_RUNS + 1),
    ).fetchall()
    return [dict(row) for row in rows[:MAX_RUNS]], len(rows) > MAX_RUNS


def hydrate_vendors(conn, runs: list[dict[str, Any]]) -> None:
    by_run: dict[int, list[dict[str, Any]]] = {int(run["id"]): [] for run in runs}
    ids = list(by_run)
    for batch in _batched(ids):
        marks = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""SELECT run_id, vendor, status, duration_ms, error
                FROM vendor_result WHERE run_id IN ({marks})
                ORDER BY run_id, vendor""",
            batch,
        ).fetchall()
        for row in rows:
            by_run[int(row["run_id"])].append({
                "vendor": row["vendor"],
                "status": row["status"] or "unknown",
                "duration_ms": row["duration_ms"] if isinstance(row["duration_ms"], int) else None,
                "failure_code": failure_code(row["error"]),
            })
    for run in runs:
        run["vendors"] = by_run[int(run["id"])]


def active_jobs(conn, *, repo_id: int | None) -> dict[str, Any]:
    where = "j.status IN ('queued','running')"
    params: list[Any] = []
    if repo_id is not None:
        where += " AND p.repo_id=?"
        params.append(repo_id)
    total = conn.execute(
        f"""SELECT COUNT(*) FROM review_job j
            JOIN pull_request p ON p.id=j.pr_id WHERE {where}""",
        params,
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT j.id, j.status, j.trigger, j.attempts, j.max_attempts,
                   j.created_at, j.locked_at, j.next_run_at, j.error,
                   p.id AS pr_id, p.number AS pr_number,
                   r.id AS repo_id, r.full_name AS repo_name
            FROM review_job j
            JOIN pull_request p ON p.id=j.pr_id
            JOIN repo r ON r.id=p.repo_id
            WHERE {where}
            ORDER BY CASE j.status WHEN 'running' THEN 0 ELSE 1 END,
                     COALESCE(j.locked_at,j.created_at), j.id
            LIMIT ?""",
        (*params, MAX_ACTIVE_JOBS),
    ).fetchall()
    return {
        "total": int(total),
        "listed": len(rows),
        "truncated": int(total) > len(rows),
        "jobs": [
            {
                "id": row["id"], "status": row["status"], "trigger": row["trigger"],
                "attempts": row["attempts"], "max_attempts": row["max_attempts"],
                "created_at": row["created_at"], "locked_at": row["locked_at"],
                "next_run_at": row["next_run_at"],
                "failure_code": failure_code(row["error"]),
                "repo": {"id": row["repo_id"], "full_name": row["repo_name"]},
                "pr": {"id": row["pr_id"], "number": row["pr_number"]},
            }
            for row in rows
        ],
    }


def summarize(runs: list[dict[str, Any]], *, truncated: bool) -> dict[str, Any]:
    statuses = Counter((run["status"] or "unknown") for run in runs)
    terminal = sum(statuses[name] for name in ("done", "failed", "canceled"))
    run_durations = [
        value for run in runs
        if (value := duration_ms(run["started_at"], run["finished_at"])) is not None
    ]
    vendor_rows = [vendor for run in runs for vendor in run["vendors"]]
    vendors: list[dict[str, Any]] = []
    for name in sorted({row["vendor"] for row in vendor_rows}):
        selected = [row for row in vendor_rows if row["vendor"] == name]
        vendor_statuses = Counter(row["status"] for row in selected)
        vendor_terminal = sum(
            vendor_statuses[key]
            for key in ("done", "failed", "partial", "timeout", "canceled")
        )
        durations = [row["duration_ms"] for row in selected if row["duration_ms"] is not None]
        vendors.append({
            "vendor": name,
            "results": len(selected),
            "statuses": dict(vendor_statuses),
            "success": {
                "numerator": vendor_statuses["done"],
                "denominator": vendor_terminal,
                "rate": _rate(vendor_statuses["done"], vendor_terminal),
            },
            "latency_ms": {
                "denominator": len(durations),
                "p50": _percentile(durations, 0.50),
                "p95": _percentile(durations, 0.95),
            },
        })

    failures = []
    for run in runs:
        failed_vendors = [row for row in run["vendors"] if row["status"] in _VENDOR_FAILURES]
        if run["status"] not in {"failed", "canceled"} and not failed_vendors:
            continue
        failures.append({
            "run_id": run["id"], "status": run["status"], "started_at": run["started_at"],
            "failure_code": failure_code(run["error"]),
            "repo": {"id": run["repo_id"], "full_name": run["repo_name"]},
            "pr": {"id": run["pr_id"], "number": run["pr_number"], "title": run["pr_title"]},
            "vendors": [
                {"vendor": row["vendor"], "status": row["status"], "failure_code": row["failure_code"]}
                for row in failed_vendors
            ],
        })
        if len(failures) >= MAX_FAILURES:
            break

    return {
        "sampled_runs": len(runs),
        "scan_limit": MAX_RUNS,
        "truncated": truncated,
        "statuses": dict(statuses),
        "success": {
            "numerator": statuses["done"],
            "denominator": terminal,
            "rate": _rate(statuses["done"], terminal),
        },
        "latency_ms": {
            "denominator": len(run_durations),
            "p50": _percentile(run_durations, 0.50),
            "p95": _percentile(run_durations, 0.95),
        },
        "vendors": vendors,
        "recent_failures": failures,
    }
