import pytest
from fastapi.testclient import TestClient

from server.api import app, get_conn
from server.context import feedback_source
from server.db import connect, init_schema
from server.repos import feedback_repo, pr_repo, repo_repo, review_repo


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _run(conn, full_name="acme/api", number=5):
    rid = repo_repo.get_by_full_name(conn, full_name)
    rid = rid["id"] if rid else repo_repo.add(conn, full_name=full_name)
    pid = pr_repo.upsert(
        conn,
        repo_id=rid,
        number=number,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    return review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )


# ---- feedback_repo write-side --------------------------------------------


def test_add_reaction_is_idempotent_per_user_emoji(db):
    run_id = _run(db)
    for _ in range(3):
        feedback_repo.add_reaction(
            db, run_id=run_id, slack_user="U1", reaction="+1", verdict="positive"
        )
    assert db.execute("SELECT COUNT(*) c FROM feedback_signal").fetchone()["c"] == 1


def test_remove_reaction_scoped_to_user_and_emoji(db):
    run_id = _run(db)
    feedback_repo.add_reaction(
        db, run_id=run_id, slack_user="U1", reaction="+1", verdict="positive"
    )
    feedback_repo.add_reaction(
        db, run_id=run_id, slack_user="U2", reaction="+1", verdict="positive"
    )
    feedback_repo.remove_reaction(db, run_id=run_id, slack_user="U1", reaction="+1")
    rows = db.execute("SELECT slack_user FROM feedback_signal").fetchall()
    assert [r["slack_user"] for r in rows] == ["U2"]


def test_run_for_message_roundtrip_and_miss(db):
    run_id = _run(db)
    feedback_repo.record_slack_post(db, run_id=run_id, channel="C1", ts="1.1")
    assert feedback_repo.run_for_message(db, channel="C1", ts="1.1") == run_id
    assert feedback_repo.run_for_message(db, channel="C1", ts="9.9") is None
    assert feedback_repo.has_slack_post(db, run_id) is True


# ---- read-side aggregation & rendering ------------------------------------


def test_slack_counts_are_repo_scoped(db):
    a = _run(db, "acme/api", 1)
    b = _run(db, "acme/web", 2)
    feedback_repo.add_reaction(
        db, run_id=a, slack_user="U1", reaction="+1", verdict="positive"
    )
    feedback_repo.add_reaction(
        db, run_id=a, slack_user="U2", reaction="-1", verdict="negative"
    )
    feedback_repo.add_reaction(
        db, run_id=b, slack_user="U3", reaction="+1", verdict="positive"
    )
    assert feedback_source.slack_counts(db, "acme/api") == {
        "positive": 1,
        "negative": 1,
    }
    assert feedback_source.slack_counts(db, "acme/web") == {
        "positive": 1,
        "negative": 0,
    }


def test_slack_feedback_line_empty_and_rendered():
    assert feedback_source.slack_feedback_line({"positive": 0, "negative": 0}) == ""
    line = feedback_source.slack_feedback_line({"positive": 8, "negative": 2})
    assert "8" in line and "2" in line and "Slack 반응" in line


def test_db_feedback_source_blends_slack_even_below_min_decisions(tmp_path):
    # finding 결정 0건(요약 게이트 미달)이라도 Slack 반응은 독립 주입된다.
    from types import SimpleNamespace

    conn = connect(tmp_path / "s.db")
    init_schema(conn)
    run_id = _run(conn)
    feedback_repo.add_reaction(
        conn, run_id=run_id, slack_user="U1", reaction="+1", verdict="positive"
    )
    source = feedback_source.db_feedback_source(db_path=str(tmp_path / "s.db"))
    text = source(SimpleNamespace(repo="acme/api"))
    assert "Slack 반응" in text


# ---- /api/learn integration ----------------------------------------------


def test_learn_surfaces_repo_with_only_slack_signal(tmp_path):
    conn = connect(tmp_path / "l.db")
    init_schema(conn)
    run_id = _run(conn, "acme/api", 7)
    feedback_repo.add_reaction(
        conn, run_id=run_id, slack_user="U1", reaction="+1", verdict="positive"
    )
    app.dependency_overrides[get_conn] = lambda: conn
    client = TestClient(app)
    out = client.get("/api/learn").json()
    assert len(out) == 1
    assert out[0]["repo"] == "acme/api"
    assert out[0]["total"] == 0
    assert out[0]["slack_reactions"] == {"positive": 1, "negative": 0}
