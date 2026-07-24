"""Bounded, privacy-safe data access for the operations canary endpoints."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from server import config

CURSOR_VERSION = 1
MAX_DAYS = 31
MAX_BASELINE_DAYS = 31
MAX_LIMIT = 100
DEFAULT_DAYS = 14
DEFAULT_LIMIT = 50
MAX_RUNS = 1_000
MAX_SCAN_RUNS = 5_000
RUN_STATUSES = frozenset({"queued", "running", "done", "failed", "canceled"})
VENDORS = frozenset({"claude", "codex"})


def normalize_filters(*, repo_id: int | None, days: int = DEFAULT_DAYS,
                      baseline_days: int | None = None, cohort: str | None = None,
                      vendor: str | None = None, status: str | None = None) -> dict[str, Any]:
    if repo_id is not None and repo_id < 1:
        raise ValueError("repo_id must be positive")
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    baseline_days = days if baseline_days is None else baseline_days
    if not 1 <= baseline_days <= MAX_BASELINE_DAYS:
        raise ValueError(f"baseline_days must be between 1 and {MAX_BASELINE_DAYS}")
    cohort = (cohort or "").strip()
    if cohort.lower() == "unknown":
        cohort = "unknown"
    if len(cohort) > 256:
        raise ValueError("cohort is too long")
    vendor = (vendor or "").strip().lower() or None
    if vendor is not None and vendor not in VENDORS:
        raise ValueError("vendor is invalid")
    status = (status or "").strip().lower() or None
    if status is not None and status not in RUN_STATUSES:
        raise ValueError("status is invalid")
    return {"repo_id": repo_id, "days": days, "baseline_days": baseline_days,
            "cohort": cohort or None, "vendor": vendor, "status": status}


def normalize_limit(limit: int = DEFAULT_LIMIT) -> int:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def _sqlite_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _now() -> str:
    return _sqlite_timestamp(datetime.now(timezone.utc))


def _filter_hash(filters: dict[str, Any]) -> str:
    # baseline_days does not change the current/run set; it remains bound so a cursor
    # cannot be reused to make a visually identical but semantically different query.
    payload = json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _cursor_key() -> bytes:
    return config.OPERATIONS_CURSOR_SECRET.encode("utf-8")


def encode_cursor(*, filters: dict[str, Any], as_of: str, snapshot_max_id: int,
                  bucket: str, started_at: str | None, run_id: int) -> str:
    payload = {"v": CURSOR_VERSION, "f": _filter_hash(filters), "a": as_of,
               "m": snapshot_max_id, "b": bucket, "s": started_at, "i": run_id}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    mac = hmac.new(_cursor_key(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + mac).decode().rstrip("=")


def decode_cursor(token: str, *, filters: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        body, supplied = raw[:-32], raw[-32:]
        if not body or not hmac.compare_digest(
            hmac.new(_cursor_key(), body, hashlib.sha256).digest(), supplied
        ):
            raise ValueError
        payload = json.loads(body)
        if (not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION
                or payload.get("f") != _filter_hash(filters)
                or payload.get("b") not in {"dated", "null"}
                or not isinstance(payload.get("a"), str)
                or not isinstance(payload.get("m"), int) or payload["m"] < 0
                or not isinstance(payload.get("i"), int)):
            raise ValueError
        if payload["b"] == "dated" and not isinstance(payload.get("s"), str):
            raise ValueError
        if payload["b"] == "null" and payload.get("s") is not None:
            raise ValueError
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("invalid or mismatched cursor") from None


def _where(filters: dict[str, Any], *, start: str | None, end: str,
           cursor: dict[str, Any] | None = None, snapshot_max_id: int | None = None,
           include_null: bool = True, end_inclusive: bool = True,
           ) -> tuple[str, list[Any]]:
    comparison = "<=" if end_inclusive else "<"
    clauses = [
        f"(rr.started_at IS NULL OR rr.started_at {comparison} ?)"
        if include_null
        else f"(rr.started_at IS NOT NULL AND rr.started_at {comparison} ?)"
    ]
    params: list[Any] = [end]
    if start is not None:
        # NULL rows are legacy timestamps and intentionally form a final explicit bucket.
        clauses.append(
            "(rr.started_at IS NULL OR rr.started_at >= ?)"
            if include_null else "rr.started_at >= ?"
        )
        params.append(start)
    if filters["repo_id"] is not None:
        clauses.append("rr.repo_id=?")
        params.append(filters["repo_id"])
    if filters["cohort"] == "unknown":
        clauses.append("(rr.policy_cohort_key IS NULL OR rr.policy_cohort_key='')")
    elif filters["cohort"]:
        clauses.append("rr.policy_cohort_key=?")
        params.append(filters["cohort"])
    if filters["vendor"]:
        clauses.append("EXISTS (SELECT 1 FROM vendor_result vf WHERE vf.run_id=rr.id AND vf.vendor=?)")
        params.append(filters["vendor"])
    if filters["status"]:
        clauses.append("rr.status=?")
        params.append(filters["status"])
    if snapshot_max_id is not None:
        clauses.append("rr.id<=?")
        params.append(snapshot_max_id)
    if cursor:
        if cursor["b"] == "dated":
            clauses.append("(rr.started_at IS NULL OR rr.started_at < ? OR (rr.started_at=? AND rr.id<?))")
            params.extend((cursor["s"], cursor["s"], cursor["i"]))
        else:
            clauses.append("rr.started_at IS NULL AND rr.id<?")
            params.append(cursor["i"])
    return " AND ".join(clauses), params


def _select_runs(conn, filters: dict[str, Any], *, start: str | None, end: str,
                 cap: int, cursor: dict[str, Any] | None = None,
                 snapshot_max_id: int | None = None,
                 include_null: bool = True,
                 end_inclusive: bool = True) -> tuple[list[dict], bool]:
    # Freeze a hard-bounded ordered candidate set before optional sparse filters.
    # Otherwise a rare cohort/vendor/status or accumulated legacy NULL rows could
    # make one management request scan the repository's entire history.
    base_filters = {**filters, "cohort": None, "vendor": None, "status": None}
    base_where, base_params = _where(
        base_filters, start=start, end=end, cursor=cursor,
        snapshot_max_id=snapshot_max_id, include_null=include_null,
        end_inclusive=end_inclusive,
    )
    candidates = conn.execute(
        f"""SELECT rr.id FROM review_run rr
             INDEXED BY idx_review_run_operations_repo
             WHERE {base_where}
             ORDER BY (rr.started_at IS NULL), rr.started_at DESC, rr.id DESC
             LIMIT ?""",
        (*base_params, MAX_SCAN_RUNS + 1),
    ).fetchall()
    scan_truncated = len(candidates) > MAX_SCAN_RUNS
    if not candidates:
        return [], scan_truncated

    where, params = _where(
        filters, start=start, end=end, cursor=cursor,
        snapshot_max_id=snapshot_max_id, include_null=include_null,
        end_inclusive=end_inclusive,
    )
    rows = conn.execute(
        f"""WITH candidates AS MATERIALIZED (
               SELECT rr.id FROM review_run rr
               INDEXED BY idx_review_run_operations_repo
               WHERE {base_where}
               ORDER BY (rr.started_at IS NULL), rr.started_at DESC, rr.id DESC
               LIMIT ?
             )
             SELECT rr.id, rr.pr_id, rr.status, rr.started_at, rr.finished_at,
                    rr.scope_requested_mode, rr.scope_effective_mode, rr.scope_policy_reason,
                    rr.dedupe_requested_mode, rr.dedupe_effective_mode, rr.dedupe_policy_reason,
                    rr.policy_cohort_key, rr.policy_decision_hash, rr.benchmark_attestation_hash
             FROM candidates c
             CROSS JOIN review_run rr ON rr.id=c.id
             WHERE {where}
             ORDER BY (rr.started_at IS NULL), rr.started_at DESC, rr.id DESC
             LIMIT ?""",
        (*base_params, MAX_SCAN_RUNS, *params, cap + 1),
    ).fetchall()
    return [dict(row) for row in rows[:cap]], scan_truncated or len(rows) > cap


def _in_clause(values: list[int]) -> tuple[str, tuple[int, ...]]:
    return ",".join("?" for _ in values), tuple(values)


def _safe_meta(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or len(value) > 65_536:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _telemetry_and_attempts(meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read only allowlisted numeric/status values from the bounded execution envelope."""
    if not meta:
        return []
    attempts = meta.get("attempts")
    if not isinstance(attempts, list):
        return [{"attempt": 1, "phase": "review", "chunks": [meta]}]
    safe = []
    for attempt in attempts[:20]:
        if not isinstance(attempt, dict) or attempt.get("phase") not in {"review", "verify"}:
            continue
        chunks = attempt.get("chunks")
        if not isinstance(chunks, list):
            continue
        safe.append({"attempt": attempt.get("attempt"), "phase": attempt["phase"],
                     "chunks": [chunk for chunk in chunks[:1000] if isinstance(chunk, dict)]})
    return safe


def hydrate_runs(
    conn, runs: list[dict], *, vendor: str | None = None
) -> list[dict]:
    if not runs:
        return runs
    ids = [row["id"] for row in runs]
    marks, values = _in_clause(ids)
    vendor_by_run: dict[int, list[dict]] = {run_id: [] for run_id in ids}
    vendor_where = " AND vendor=?" if vendor else ""
    vendor_values = (*values, vendor) if vendor else values
    for row in conn.execute(
        f"""SELECT run_id, vendor, status, duration_ms, execution_meta
            FROM vendor_result WHERE run_id IN ({marks}){vendor_where}""",
        vendor_values,
    ):
        meta = _safe_meta(row["execution_meta"])
        vendor_by_run[row["run_id"]].append({"vendor": row["vendor"], "status": row["status"] or "unknown",
                                                "duration_ms": row["duration_ms"], "attempts": _telemetry_and_attempts(meta)})
    findings: dict[int, list[dict]] = {run_id: [] for run_id in ids}
    for row in conn.execute(
        f"""SELECT run_id, status, scope_status, posting_eligible,
                   duplicate_group_id, duplicate_suggested
            FROM finding WHERE run_id IN ({marks}){vendor_where}""",
        vendor_values,
    ):
        findings[row["run_id"]].append(dict(row))
    for run in runs:
        run["vendors"] = vendor_by_run[run["id"]]
        run["findings"] = findings[run["id"]]
    return runs


def _empty_metrics() -> dict[str, Any]:
    return {"runs": 0, "policy_modes": {"observe": 0, "enforce": 0, "unknown": 0},
            "vendor_final": {"denominator": 0, "statuses": {}},
            "vendor_attempts": {"denominator": 0, "statuses": {}, "phases": {}},
            "telemetry": {"denominator": 0, "ok": 0, "partial": 0, "unavailable": 0},
            "aggregates": {"tokens": 0, "tools": 0, "duration_ms": 0,
                           "token_distribution": [], "tool_distribution": [], "duration_distribution_ms": []},
            "scope": {"owned": 0, "reassigned": 0, "would_reject": 0, "rejected": 0},
            "posting": {"eligible": 0, "suppressed": 0},
            "duplicates": {"groups": 0, "originals": 0, "note": "operational observation, not precision"},
            "adjudication": {"coverage_denominator": 0, "decided": 0,
                              "would_reject_feedback_denominator": 0, "approved": 0, "edited": 0, "dismissed": 0}}


def metrics(runs: list[dict]) -> dict[str, Any]:
    out = _empty_metrics()
    out["runs"] = len(runs)
    final, attempts, phases = Counter(), Counter(), Counter()
    telemetry = Counter()
    tokens: list[int] = []; tools: list[int] = []; durations: list[int] = []
    duplicate_groups: set[tuple[int, int]] = set()
    for run in runs:
        scope = run.get("scope_effective_mode"); dedupe = run.get("dedupe_effective_mode")
        mode = "enforce" if "enforce" in {scope, dedupe} else "observe" if scope or dedupe else "unknown"
        out["policy_modes"][mode] += 1
        for vr in run["vendors"]:
            final[vr["status"]] += 1
            if isinstance(vr["duration_ms"], int) and vr["duration_ms"] >= 0:
                durations.append(vr["duration_ms"])
            all_chunks = [chunk for attempt in vr["attempts"] for chunk in attempt["chunks"]]
            if not all_chunks:
                telemetry["unavailable"] += 1
            for attempt in vr["attempts"]:
                phase = attempt["phase"]
                label = "initial" if phase == "review" and attempt.get("attempt") == 1 else "retry" if phase == "review" else "verify"
                phases[label] += 1
                chunk_statuses = [
                    chunk.get("status") for chunk in attempt["chunks"]
                    if isinstance(chunk.get("status"), str)
                ]
                if not chunk_statuses:
                    attempt_status = "unavailable"
                elif all(status == "done" for status in chunk_statuses):
                    attempt_status = "done"
                elif any(status == "done" for status in chunk_statuses):
                    attempt_status = "partial"
                elif all(status == "timeout" for status in chunk_statuses):
                    attempt_status = "timeout"
                elif all(status == "canceled" for status in chunk_statuses):
                    attempt_status = "canceled"
                else:
                    attempt_status = "failed"
                attempts[attempt_status] += 1
                for chunk in attempt["chunks"]:
                    state = chunk.get("telemetry_status")
                    telemetry[state if state in {"ok", "partial", "unavailable"} else "unavailable"] += 1
                    for key, target in (("total_tokens", tokens), ("tool_calls", tools)):
                        value = chunk.get(key)
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0: target.append(value)
        for finding in run["findings"]:
            scope_status = finding.get("scope_status")
            if scope_status in out["scope"]: out["scope"][scope_status] += 1
            if finding.get("posting_eligible"): out["posting"]["eligible"] += 1
            else: out["posting"]["suppressed"] += 1
            gid = finding.get("duplicate_group_id")
            if isinstance(gid, int):
                duplicate_groups.add((int(run["id"]), gid))
            if finding.get("status") != "pending": out["adjudication"]["decided"] += 1
            out["adjudication"]["coverage_denominator"] += 1
            if scope_status == "would_reject" or not finding.get("posting_eligible"):
                out["adjudication"]["would_reject_feedback_denominator"] += 1
                if finding.get("status") in {"approved", "edited", "dismissed"}:
                    out["adjudication"][finding["status"]] += 1
    out["duplicates"]["groups"] = len(duplicate_groups)
    # A duplicate group has one original operational finding; this is not a
    # precision measurement and does not infer semantic duplicate truth.
    out["duplicates"]["originals"] = len(duplicate_groups)
    out["vendor_final"] = {"denominator": sum(final.values()), "statuses": dict(final)}
    out["vendor_attempts"] = {"denominator": sum(attempts.values()), "statuses": dict(attempts), "phases": dict(phases)}
    out["telemetry"] = {"denominator": sum(telemetry.values()), **{key: telemetry[key] for key in ("ok", "partial", "unavailable")}}
    out["aggregates"] = {"tokens": sum(tokens), "tools": sum(tools), "duration_ms": sum(durations),
                         "token_distribution": sorted(tokens)[:MAX_RUNS], "tool_distribution": sorted(tools)[:MAX_RUNS],
                         "duration_distribution_ms": sorted(durations)[:MAX_RUNS]}
    return out


def public_runs(runs: list[dict]) -> list[dict]:
    result = []
    for run in runs:
        finding_counts = Counter(f.get("scope_status") or "unknown" for f in run["findings"])
        result.append({"id": run["id"], "status": run["status"], "started_at": run["started_at"],
                       "finished_at": run["finished_at"], "cohort": run["policy_cohort_key"] or "unknown",
                       "policy": {"scope_requested_mode": run["scope_requested_mode"], "scope_effective_mode": run["scope_effective_mode"],
                                  "scope_reason": run["scope_policy_reason"], "dedupe_requested_mode": run["dedupe_requested_mode"],
                                  "dedupe_effective_mode": run["dedupe_effective_mode"], "dedupe_reason": run["dedupe_policy_reason"]},
                       "vendor_final": {"denominator": len(run["vendors"]), "statuses": dict(Counter(v["status"] for v in run["vendors"]))},
                       "finding_scope": dict(finding_counts)})
    return result


def windows(filters: dict[str, Any], *, as_of: str | None = None) -> tuple[str, str, str]:
    as_of = as_of or _now()
    parsed = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current_start = _sqlite_timestamp(parsed - timedelta(days=filters["days"]))
    baseline_start = _sqlite_timestamp(
        parsed - timedelta(days=filters["days"] + filters["baseline_days"])
    )
    as_of = _sqlite_timestamp(parsed)
    return as_of, current_start, baseline_start
