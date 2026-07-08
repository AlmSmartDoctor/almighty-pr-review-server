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
    assert row["run_id"] == run_id
    assert row["severity"] == "high"
