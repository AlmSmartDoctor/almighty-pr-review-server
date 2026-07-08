from fastapi.testclient import TestClient

from server.api import app, get_conn, get_gh
from server.db import connect, init_schema
from server.repos import repo_repo, pr_repo, review_repo, finding_repo

import pytest


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_post_only_approved_findings(tmp_path):
    conn = connect(tmp_path / "p.db")
    init_schema(conn)
    posted = []

    class FakeGh:
        def post_comment(self, repo, number, body):
            posted.append((repo, number, body))
            return {"id": 1, "html_url": "https://x/1#issuecomment-1"}

    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_gh] = lambda: FakeGh()
    client = TestClient(app)

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=5,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    f_ok = finding_repo.add(
        conn,
        run_id=run_id,
        vendor="claude",
        file="a.py",
        line=1,
        severity="high",
        category="bug",
        claim="c",
        rationale="r",
        confidence=0.9,
    )
    f_no = finding_repo.add(
        conn,
        run_id=run_id,
        vendor="claude",
        file="b.py",
        line=2,
        severity="low",
        category="style",
        claim="c2",
        rationale="r2",
        confidence=0.2,
    )
    finding_repo.set_status(conn, f_ok, "approved")
    finding_repo.set_status(conn, f_no, "dismissed")

    r = client.post(f"/api/runs/{run_id}/post")
    assert r.status_code == 200
    # 승인분만 코멘트 본문에 포함
    assert posted and "a.py:1" in posted[0][2]
    assert "b.py:2" not in posted[0][2]
    # 포스팅된 finding은 status=posted
    assert finding_repo.get(conn, f_ok)["status"] == "posted"
