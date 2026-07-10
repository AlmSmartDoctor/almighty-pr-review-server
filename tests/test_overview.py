import pytest
from fastapi.testclient import TestClient

from server.api import app, get_conn
from server.db import connect, init_schema


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_overview_lists_open_pr_with_top_severity(tmp_path):
    conn = connect(tmp_path / "api.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)

    from server.repos import finding_repo, pr_repo, repo_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=7,
        title="Add feature",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
        created_at="2026-07-07T11:22:33Z",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    finding_repo.add(
        conn,
        run_id=run_id,
        vendor="claude",
        file="a",
        line=1,
        severity="high",
        category="bug",
        claim="c",
        rationale="r",
        confidence=0.8,
    )

    rows = client.get("/api/overview").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Add feature"
    assert row["repo"] == "acme/api"
    assert row["author"] == "a"
    assert row["created_at"] == "2026-07-07T11:22:33Z"
    assert row["first_seen_at"] is not None
    assert row["run_id"] == run_id
    assert row["run_status"] == "running"
    assert row["run_error"] is None
    assert row["severity"] == "high"


def test_overview_severity_is_worst_across_findings(tmp_path):
    conn = connect(tmp_path / "api.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)

    from server.repos import finding_repo, pr_repo, repo_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=7,
        title="Add feature",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    finding_repo.add(
        conn,
        run_id=run_id,
        vendor="claude",
        file="a",
        line=1,
        severity="high",
        category="bug",
        claim="c",
        rationale="r",
        confidence=0.8,
    )
    finding_repo.add(
        conn,
        run_id=run_id,
        vendor="claude",
        file="b",
        line=2,
        severity="critical",
        category="bug",
        claim="c",
        rationale="r",
        confidence=0.8,
    )

    rows = client.get("/api/overview").json()
    assert len(rows) == 1
    assert rows[0]["severity"] == "critical"


def test_overview_no_findings_defaults_low(tmp_path):
    conn = connect(tmp_path / "api.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)

    from server.repos import pr_repo, repo_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=7,
        title="Add feature",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )

    rows = client.get("/api/overview").json()
    assert len(rows) == 1
    assert rows[0]["severity"] == "low"
    assert rows[0]["run_id"] == run_id


def test_overview_exposes_latest_run_status_and_error(tmp_path):
    conn = connect(tmp_path / "api.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)

    from server.repos import pr_repo, repo_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=7,
        title="Add feature",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    review_repo.create_run(
        conn, pr_id=pid, head_sha="old", trigger="manual", effort="medium"
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    review_repo.finish_run(conn, run_id, "canceled", error="diff too large")

    rows = client.get("/api/overview").json()
    assert rows[0]["run_id"] == run_id
    assert rows[0]["run_status"] == "canceled"
    assert rows[0]["run_error"] == "diff too large"
