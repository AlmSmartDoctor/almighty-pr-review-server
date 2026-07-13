from fastapi.testclient import TestClient

from server.api import app, get_conn, get_gh
from server.db import connect, init_schema
from server.github.gh import GitHubCliError
from server.repos import repo_repo, pr_repo, review_repo, finding_repo

import pytest


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


class HealthyGh:
    def preflight_user(self):
        return {"login": "me"}

    def preflight_repo(self, repo):
        return {"full_name": repo}

    def preflight_issue(self, repo, number):
        return {"number": number}


def test_post_only_approved_findings(tmp_path):
    conn = connect(tmp_path / "p.db")
    init_schema(conn)
    posted = []

    class FakeGh(HealthyGh):
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


def test_post_includes_edited_findings_with_edited_text(tmp_path):
    conn = connect(tmp_path / "edited.db")
    init_schema(conn)
    posted = []

    class FakeGh(HealthyGh):
        def post_comment(self, repo, number, body):
            posted.append(body)
            return {"id": 1, "html_url": "https://x/1"}

    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_gh] = lambda: FakeGh()
    client = TestClient(app)

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=9,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    fid = finding_repo.add(
        conn,
        run_id=run_id,
        vendor="claude",
        file="a.py",
        line=1,
        severity="high",
        category="bug",
        claim="original claim",
        rationale="r",
        confidence=0.9,
    )
    finding_repo.set_status(conn, fid, "edited", edited_text="edited claim")

    r = client.post(f"/api/runs/{run_id}/post")

    assert r.status_code == 200
    assert posted and "edited claim" in posted[0]
    assert "original claim" not in posted[0]
    assert finding_repo.get(conn, fid)["status"] == "posted"


def test_post_preview_uses_formatter_output(tmp_path):
    conn = connect(tmp_path / "preview.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=10,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    fid = finding_repo.add(
        conn,
        run_id=run_id,
        vendor="codex",
        file="b.py",
        line=2,
        severity="medium",
        category="bug",
        claim="preview claim",
        rationale="r",
        confidence=0.7,
    )
    finding_repo.set_status(conn, fid, "approved")

    r = client.get(f"/api/runs/{run_id}/post-preview")

    assert r.status_code == 200
    comments = r.json()["comments"]
    assert comments[0]["vendor"] == "codex"
    assert "<!-- almighty-review [codex] -->" in comments[0]["body"]
    assert "preview claim" in comments[0]["body"]


def test_post_updates_existing_comment_in_place(tmp_path):
    conn = connect(tmp_path / "p2.db")
    init_schema(conn)
    posted, edited = [], []

    class FakeGh(HealthyGh):
        def post_comment(self, repo, number, body):
            posted.append((repo, number, body))
            return {"id": 7, "html_url": "https://x/7"}

        def edit_comment(self, repo, comment_id, body):
            edited.append((repo, comment_id, body))
            return {"id": int(comment_id), "html_url": f"https://x/{comment_id}"}

    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_gh] = lambda: FakeGh()
    client = TestClient(app)

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=6,
        title="t",
        author="a",
        head_sha="s1",
        base_ref="main",
        url="u",
    )

    run1 = review_repo.create_run(
        conn, pr_id=pid, head_sha="s1", trigger="manual", effort="medium"
    )
    f1 = finding_repo.add(
        conn,
        run_id=run1,
        vendor="claude",
        file="a.py",
        line=1,
        severity="high",
        category="bug",
        claim="c",
        rationale="r",
        confidence=0.9,
    )
    finding_repo.set_status(conn, f1, "approved")
    client.post(f"/api/runs/{run1}/post")
    assert len(posted) == 1 and edited == []  # 첫 리뷰 → create

    run2 = review_repo.create_run(
        conn, pr_id=pid, head_sha="s2", trigger="manual", effort="medium"
    )
    f2 = finding_repo.add(
        conn,
        run_id=run2,
        vendor="claude",
        file="a.py",
        line=1,
        severity="high",
        category="bug",
        claim="c2",
        rationale="r2",
        confidence=0.9,
    )
    finding_repo.set_status(conn, f2, "approved")
    client.post(f"/api/runs/{run2}/post")
    assert len(posted) == 1  # 새 post 없음
    assert len(edited) == 1 and edited[0][1] == "7"  # 이전 gcid로 in-place edit
    rows = conn.execute(
        "SELECT superseded_at FROM posted_comment ORDER BY id"
    ).fetchall()
    assert rows[0]["superseded_at"] is not None  # 이전 행 supersede
    assert rows[1]["superseded_at"] is None  # 새 행 활성


def test_post_groups_by_vendor(tmp_path):
    conn = connect(tmp_path / "p3.db")
    init_schema(conn)
    posted = []

    class FakeGh(HealthyGh):
        def post_comment(self, repo, number, body):
            posted.append(body)
            return {"id": len(posted), "html_url": "https://x"}

    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_gh] = lambda: FakeGh()
    client = TestClient(app)

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=8,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    fc = finding_repo.add(
        conn,
        run_id=run_id,
        vendor="claude",
        file="a.py",
        line=1,
        severity="high",
        category="bug",
        claim="cc",
        rationale="r",
        confidence=0.9,
    )
    fx = finding_repo.add(
        conn,
        run_id=run_id,
        vendor="codex",
        file="b.py",
        line=2,
        severity="low",
        category="style",
        claim="xx",
        rationale="r",
        confidence=0.5,
    )
    finding_repo.set_status(conn, fc, "approved")
    finding_repo.set_status(conn, fx, "approved")
    r = client.post(f"/api/runs/{run_id}/post")
    assert r.status_code == 200
    assert len(posted) == 2  # 벤더별 코멘트 분리


def _run_with_approved(conn, *, vendors=("claude",)):
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=8,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    for vendor in vendors:
        fid = finding_repo.add(
            conn,
            run_id=run_id,
            vendor=vendor,
            file=f"{vendor}.py",
            line=1,
            severity="high" if vendor == "claude" else "low",
            category="bug",
            claim=f"{vendor} claim",
            rationale="r",
            confidence=0.9,
        )
        finding_repo.set_status(conn, fid, "approved")
    return pid, run_id


def test_post_health_success(tmp_path):
    conn = connect(tmp_path / "health-ok.db")
    init_schema(conn)
    pid, _ = _run_with_approved(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_gh] = lambda: HealthyGh()
    client = TestClient(app)

    r = client.get(f"/api/prs/{pid}/post-health")

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["auth"]["login"] == "me"


def test_post_health_auth_failure_is_structured(tmp_path):
    conn = connect(tmp_path / "health-auth.db")
    init_schema(conn)
    pid, _ = _run_with_approved(conn)

    class FakeGh:
        def preflight_user(self):
            raise GitHubCliError(
                exit_code=1,
                message="HTTP 401: bad credentials",
                stderr="HTTP 401: bad credentials",
                command_kind="preflight_user",
                http_status=401,
            )

    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_gh] = lambda: FakeGh()
    client = TestClient(app)

    r = client.get(f"/api/prs/{pid}/post-health")

    assert r.status_code == 401
    body = r.json()
    assert body["ok"] is False
    assert body["auth"]["ok"] is False
    assert "인증" in body["message"]


def test_post_preflight_failure_does_not_write(tmp_path):
    conn = connect(tmp_path / "post-auth.db")
    init_schema(conn)
    _, run_id = _run_with_approved(conn)
    calls = []

    class FakeGh:
        def preflight_user(self):
            raise GitHubCliError(
                exit_code=1,
                message="HTTP 401: bad credentials",
                stderr="HTTP 401: bad credentials",
                command_kind="preflight_user",
                http_status=401,
            )

        def post_comment(self, repo, number, body):
            calls.append((repo, number, body))

    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_gh] = lambda: FakeGh()
    client = TestClient(app)

    r = client.post(f"/api/runs/{run_id}/post")

    assert r.status_code == 401
    assert calls == []
    assert r.json()["detail"]["auth"]["ok"] is False


def test_post_partial_vendor_failure_returns_posted_and_failed_detail(tmp_path):
    conn = connect(tmp_path / "post-partial.db")
    init_schema(conn)
    _, run_id = _run_with_approved(conn, vendors=("claude", "codex"))

    class FakeGh(HealthyGh):
        def post_comment(self, repo, number, body):
            if "codex claim" in body:
                raise GitHubCliError(
                    exit_code=1,
                    message="HTTP 403: forbidden",
                    stderr="HTTP 403: forbidden",
                    command_kind="post_comment",
                    http_status=403,
                )
            return {"id": 1, "html_url": "https://x/1"}

    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_gh] = lambda: FakeGh()
    client = TestClient(app)

    r = client.post(f"/api/runs/{run_id}/post")

    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["posted"] == [{"vendor": "claude", "url": "https://x/1"}]
    assert detail["failed"]["vendor"] == "codex"
    assert conn.execute("SELECT COUNT(*) AS n FROM posted_comment").fetchone()["n"] == 1
