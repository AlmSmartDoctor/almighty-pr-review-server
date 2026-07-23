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


def test_overview_prescreen_matches_current_pr_head(tmp_path):
    conn = connect(tmp_path / "api.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)

    from server.repos import prescreen_repo, pr_repo, repo_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=9,
        title="New head running",
        author="a",
        head_sha="old",
        base_ref="main",
        url="u",
    )
    prescreen_repo.add(
        conn,
        pr_id=pid,
        head_sha="old",
        model="haiku",
        complexity="complex",
        score=0.9,
        reason="old result",
        duration_ms=7,
        decided="review",
    )
    pr_repo.upsert(
        conn,
        repo_id=rid,
        number=9,
        title="New head running",
        author="a",
        head_sha="new",
        base_ref="main",
        url="u",
    )
    latest_run = review_repo.create_run(
        conn, pr_id=pid, head_sha="new", trigger="manual", effort="medium"
    )

    row = client.get("/api/overview").json()[0]

    assert row["run_id"] == latest_run
    assert row["run_status"] == "running"
    assert row["prescreen"] is None
    assert row["prescreen_duration_ms"] is None


def test_overview_finding_summary_uses_latest_run_only(tmp_path):
    conn = connect(tmp_path / "api.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)

    from server.repos import finding_repo, pr_repo, repo_repo, review_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=8,
        title="Clean after re-review",
        author="a",
        head_sha="new",
        base_ref="main",
        url="u",
    )
    old_run = review_repo.create_run(
        conn, pr_id=pid, head_sha="old", trigger="manual", effort="medium"
    )
    finding_repo.add(
        conn,
        run_id=old_run,
        vendor="claude",
        file="a.py",
        line=1,
        severity="critical",
        category="bug",
        claim="old issue",
        rationale="fixed later",
        confidence=0.9,
    )
    review_repo.finish_run(conn, old_run, "done")
    latest_run = review_repo.create_run(
        conn, pr_id=pid, head_sha="new", trigger="manual", effort="medium"
    )
    review_repo.finish_run(conn, latest_run, "done")

    row = client.get("/api/overview").json()[0]

    assert row["run_id"] == latest_run
    assert row["finding_count"] == 0
    assert row["sev_rank"] is None
    assert row["severity"] == "low"


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


def test_overview_exposes_latest_job_status_for_prerun_failures(tmp_path):
    # run 생성 전 실패(build_deps 등)와 backoff 대기는 review_run에 안 남는다 —
    # 최신 job 상태를 노출해야 "잡을 큐에 넣었습니다" 후 무소식 블랙박스가 사라진다.
    conn = connect(tmp_path / "api.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)

    from server.repos import job_repo, pr_repo, repo_repo

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
    job_id = job_repo.enqueue(conn, pr_id=pid, head_sha="s", trigger="auto")
    conn.execute(
        "UPDATE review_job SET status='failed', error='clone 실패' WHERE id=?",
        (job_id,),
    )
    conn.commit()

    rows = client.get("/api/overview").json()
    assert rows[0]["job_status"] == "failed"
    assert rows[0]["job_error"] == "clone 실패"
    assert rows[0]["run_id"] is None  # run은 없음 — job만이 실패를 안다


def test_overview_uses_job_for_current_head_when_pr_returns_to_old_sha(tmp_path):
    conn = connect(tmp_path / "reverted-head.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)

    from server.repos import job_repo, pr_repo, repo_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn, repo_id=rid, number=8, title="Reset", author="a", head_sha="old",
        base_ref="main", url="u",
    )
    old_job = job_repo.enqueue(conn, pr_id=pid, head_sha="old", trigger="auto")
    conn.execute("UPDATE review_job SET status='done' WHERE id=?", (old_job,))
    conn.commit()

    pr_repo.upsert(
        conn, repo_id=rid, number=8, title="Reset", author="a", head_sha="new",
        base_ref="main", url="u",
    )
    new_job = job_repo.enqueue(conn, pr_id=pid, head_sha="new", trigger="auto")
    conn.execute(
        "UPDATE review_job SET status='failed', error='new head failure' WHERE id=?",
        (new_job,),
    )
    conn.commit()

    pr_repo.upsert(
        conn, repo_id=rid, number=8, title="Reset", author="a", head_sha="old",
        base_ref="main", url="u",
    )
    job_repo.enqueue_manual(conn, pr_id=pid, head_sha="old")

    row = client.get("/api/overview").json()[0]
    assert row["job_status"] == "queued"
    assert row["job_error"] is None


def test_overview_orders_by_created_at_desc_and_exposes_draft(tmp_path):
    conn = connect(tmp_path / "api.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)

    from server.repos import pr_repo, repo_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    common = dict(repo_id=rid, author="a", head_sha="s", base_ref="main", url="u")
    pr_repo.upsert(
        conn, number=1, title="oldest", created_at="2026-07-01T00:00:00Z", **common
    )
    pr_repo.upsert(
        conn, number=2, title="newest", created_at="2026-07-09T00:00:00Z", **common
    )
    pr_repo.upsert(
        conn,
        number=3,
        title="mid draft",
        created_at="2026-07-05T00:00:00Z",
        is_draft=True,
        **common,
    )

    rows = client.get("/api/overview").json()
    assert [r["number"] for r in rows] == [
        2,
        3,
        1,
    ]  # created_at DESC(폴 노이즈 updated_at 아님)
    by_num = {r["number"]: r for r in rows}
    assert by_num[3]["is_draft"] == 1  # draft 상태 노출
    assert by_num[2]["is_draft"] == 0


def test_overview_sort_normalizes_mixed_timestamp_formats(tmp_path):
    # created_at(ISO 'T'/'Z')와 first_seen_at(공백 구분) 포맷이 섞여도 실제 시각순 정렬.
    # webhook 유입 PR은 다음 폴 전까지 created_at=NULL → first_seen_at 폴백.
    conn = connect(tmp_path / "api.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)

    from server.repos import pr_repo, repo_repo

    rid = repo_repo.add(conn, full_name="acme/api")
    common = dict(repo_id=rid, author="a", head_sha="s", base_ref="main", url="u")
    # PR1: 같은 날 00:00에 생성된 poller PR
    pr_repo.upsert(
        conn, number=1, title="poller 00시", created_at="2026-07-08T00:00:00Z", **common
    )
    # PR2: created_at 없는 webhook PR — first_seen_at을 같은 날 12:00로 강제(더 최신)
    pid2 = pr_repo.upsert(
        conn, number=2, title="webhook 12시", created_at=None, **common
    )
    conn.execute(
        "UPDATE pull_request SET first_seen_at='2026-07-08 12:00:00' WHERE id=?",
        (pid2,),
    )
    conn.commit()

    rows = client.get("/api/overview").json()
    # 문자열 비교였다면 'T'(84)>' '(32)로 PR1이 위 → 순수 문자열 정렬은 [1,2](버그).
    # datetime() 정규화로 실제 시각순 → 12시 webhook PR이 위.
    assert [r["number"] for r in rows] == [2, 1]
