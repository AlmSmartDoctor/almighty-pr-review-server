"""Read-only, bounded operations metrics routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from server import config
from server.api import get_conn
from server.repos import canary_repo

router = APIRouter(prefix="/api/operations", tags=["operations"])


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _benchmark_public() -> dict:
    """Expose benchmark fields only after the attestation verifier accepts them."""
    from server.review.benchmark_attestation import (
        public_benchmark_evidence,
        resolve_benchmark_attestation,
    )

    decision = resolve_benchmark_attestation()
    evidence = public_benchmark_evidence()
    if evidence is None:
        return {
            "validated": False, "status": decision.reason.value,
            "report_hash": None, "identity": None, "sample": None,
            "generated_at": None, "metrics": None,
            "cost_regression": None, "gate_reasons": [],
        }
    identity = evidence["identity"]
    return {
        "validated": decision.can_enforce,
        "status": decision.reason.value,
        "report_hash": evidence["report_hash"],
        "identity": {
            field: getattr(identity, field)
            for field in identity.__dataclass_fields__
        },
        "sample": evidence["sample"],
        "generated_at": evidence["generated_at"],
        "metrics": evidence["metrics"],
        "cost_regression": evidence["cost_regression"],
        "gate_reasons": evidence["gate_reasons"],
    }


def _control_public(conn, repo_id: int) -> dict:
    row = conn.execute(
        """SELECT full_name, review_scope_guard_mode, review_dedupe_mode
           FROM repo WHERE id=?""",
        (repo_id,),
    ).fetchone()
    full_name = row["full_name"] if row else ""

    def policy(name: str, global_mode: str, canaries, kill_switch: bool) -> dict:
        override = row[f"review_{name}_mode"] if row else None
        explicit = override if override in {"observe", "enforce"} else None
        configured = explicit or global_mode
        return {
            "configured_mode": configured,
            "selection_source": "repo_override" if explicit else "global_default",
            "canary_member": (
                explicit == "enforce"
                or (explicit is None and full_name in canaries)
            ),
            "kill_switch": kill_switch,
        }

    return {
        "enforcement_unlocked": config.REVIEW_POLICY_ENFORCEMENT_UNLOCKED,
        "scope": policy(
            "scope_guard", config.REVIEW_SCOPE_GUARD_MODE,
            config.REVIEW_SCOPE_ENFORCE_REPOS, config.REVIEW_SCOPE_KILL_SWITCH,
        ),
        "dedupe": policy(
            "dedupe", config.REVIEW_DEDUPE_MODE,
            config.REVIEW_DEDUPE_ENFORCE_REPOS, config.REVIEW_DEDUPE_KILL_SWITCH,
        ),
        "configuration_activation": "startup",
        "restart_required": True,
    }


def _filters(repo_id, days, baseline_days, cohort, vendor, status):
    try:
        return canary_repo.normalize_filters(repo_id=repo_id, days=days,
            baseline_days=baseline_days, cohort=cohort, vendor=vendor, status=status)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/review-policy/summary")
def review_policy_summary(
    repo_id: int = Query(..., ge=1), days: int = Query(default=canary_repo.DEFAULT_DAYS),
    cohort: str | None = Query(default=None), vendor: str | None = Query(default=None),
    status: str | None = Query(default=None), baseline_days: int | None = Query(default=None),
    conn=Depends(get_conn),
):
    filters = _filters(repo_id, days, baseline_days, cohort, vendor, status)
    as_of, current_start, baseline_start = canary_repo.windows(filters)
    current, current_truncated = canary_repo._select_runs(
        conn, filters, start=current_start, end=as_of, cap=canary_repo.MAX_RUNS
    )
    baseline, baseline_truncated = canary_repo._select_runs(
        conn, filters, start=baseline_start, end=current_start,
        cap=canary_repo.MAX_RUNS, include_null=False, end_inclusive=False,
    )
    canary_repo.hydrate_runs(conn, current, vendor=filters["vendor"])
    canary_repo.hydrate_runs(conn, baseline, vendor=filters["vendor"])
    current_metrics = canary_repo.metrics(current)
    baseline_metrics = canary_repo.metrics(baseline)
    minimum = 20
    baseline_shortfall = max(0, minimum - baseline_metrics["runs"])
    current_shortfall = max(0, minimum - current_metrics["runs"])
    # A first cohort window cannot establish a comparison.  This is explicitly not a
    # positive safety signal.
    comparison_status = "ready" if not baseline_shortfall and not current_shortfall else "insufficient_baseline"
    sampled = max((row["started_at"] for row in current if row["started_at"]), default=None)
    return {"filters": filters, "as_of": as_of, "sampled_through": sampled,
            "max_runs": canary_repo.MAX_RUNS, "truncated": current_truncated or baseline_truncated,
            "current": current_metrics,
            "baseline": {"window_days": filters["baseline_days"], "metrics": baseline_metrics,
                         "truncated": baseline_truncated},
            "comparison": {"status": comparison_status, "minimum_denominator": minimum,
                           "current_run_shortfall": current_shortfall,
                           "baseline_run_shortfall": baseline_shortfall},
            "benchmark": _benchmark_public(),
            "control": _control_public(conn, filters["repo_id"])}


@router.get("/review-policy/runs")
def review_policy_runs(
    repo_id: int = Query(..., ge=1), days: int = Query(default=canary_repo.DEFAULT_DAYS),
    cohort: str | None = Query(default=None), vendor: str | None = Query(default=None),
    status: str | None = Query(default=None), cursor: str | None = Query(default=None),
    limit: int = Query(default=canary_repo.DEFAULT_LIMIT),
    conn=Depends(get_conn),
):
    filters = _filters(repo_id, days, None, cohort, vendor, status)
    try:
        limit = canary_repo.normalize_limit(limit)
        decoded = canary_repo.decode_cursor(cursor, filters=filters) if cursor else None
    except ValueError as exc:
        raise _bad_request(exc) from exc
    as_of, current_start, _ = canary_repo.windows(filters, as_of=decoded["a"] if decoded else None)
    # review_run has no creation timestamp; its monotonically increasing primary key
    # forms the immutable snapshot ceiling so rows inserted after page one cannot leak
    # into a continuation even if their started_at is backdated.
    snapshot_max_id = decoded["m"] if decoded else conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM review_run"
    ).fetchone()[0]
    runs, more = canary_repo._select_runs(conn, filters, start=current_start, end=as_of,
                                          cap=limit, cursor=decoded, snapshot_max_id=snapshot_max_id)
    canary_repo.hydrate_runs(conn, runs, vendor=filters["vendor"])
    next_cursor = None
    if more and runs:
        last = runs[-1]
        next_cursor = canary_repo.encode_cursor(filters=filters, as_of=as_of,
            snapshot_max_id=snapshot_max_id,
            bucket="null" if last["started_at"] is None else "dated",
            started_at=last["started_at"], run_id=last["id"])
    sampled = max((row["started_at"] for row in runs if row["started_at"]), default=None)
    return {"filters": filters, "as_of": as_of, "sampled_through": sampled,
            "limit": limit, "max_runs": canary_repo.MAX_RUNS, "truncated": more,
            "denominator": len(runs), "runs": canary_repo.public_runs(runs), "next_cursor": next_cursor}
