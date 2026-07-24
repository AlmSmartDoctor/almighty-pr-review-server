"""Read-only, bounded review system operations dashboard routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from server.api import get_conn
from server.repos import operations_repo

router = APIRouter(prefix="/api/operations", tags=["operations"])


@router.get("/dashboard")
def operations_dashboard(
    repo_id: int | None = Query(default=None, ge=1),
    range: str = Query(default="24h"),
    conn=Depends(get_conn),
):
    try:
        filters = operations_repo.normalize_filters(repo_id=repo_id, horizon=range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    as_of, start = operations_repo.window(filters)
    runs, truncated = operations_repo.select_runs(
        conn, filters, start=start, end=as_of
    )
    operations_repo.hydrate_vendors(conn, runs)
    summary = operations_repo.summarize(runs, truncated=truncated)
    return {
        "filters": filters,
        "as_of": as_of,
        "window_start": start,
        "summary": summary,
        "active_jobs": operations_repo.active_jobs(conn, repo_id=repo_id),
    }
