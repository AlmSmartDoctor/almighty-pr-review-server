from server.context.base import ContextRequest
from server.db import connect, init_schema
from server.repos import repo_repo


def _db(tmp_path):
    conn = connect(tmp_path / "rules.db")
    init_schema(conn)
    return conn


def test_proposals_require_repeated_majority_rejection_and_never_auto_activate(tmp_path):
    from server.repos import review_rule_repo

    conn = _db(tmp_path)
    rid = repo_repo.add(conn, full_name="acme/api")
    rules = review_rule_repo.propose_rules(
        conn,
        rid,
        [
            {"category": "style", "approved": 0, "edited": 1, "rejected": 3},
            {"category": "bug", "approved": 2, "edited": 0, "rejected": 1},
            {"category": "perf", "approved": 0, "edited": 0, "rejected": 2},
        ],
    )

    assert len(rules) == 1
    assert rules[0]["category"] == "style"
    assert rules[0]["status"] == "proposed"
    assert rules[0]["evidence_total"] == 4
    assert rules[0]["evidence_rejected"] == 3
    assert "동작 영향" in rules[0]["text"]


def test_reproposal_preserves_explicit_active_or_disabled_status(tmp_path):
    from server.repos import review_rule_repo

    conn = _db(tmp_path)
    rid = repo_repo.add(conn, full_name="acme/api")
    categories = [{"category": "style", "approved": 0, "edited": 0, "rejected": 3}]
    rule = review_rule_repo.propose_rules(conn, rid, categories)[0]
    assert review_rule_repo.set_status(conn, rule["id"], "active")["status"] == "active"

    refreshed = review_rule_repo.propose_rules(
        conn,
        rid,
        [{"category": "style", "approved": 1, "edited": 0, "rejected": 4}],
    )[0]
    assert refreshed["status"] == "active"
    assert refreshed["evidence_total"] == 5

    assert review_rule_repo.set_status(conn, rule["id"], "disabled")["status"] == "disabled"
    assert review_rule_repo.propose_rules(conn, rid, categories)[0]["status"] == "disabled"


def test_rule_status_rejects_unknown_state_and_missing_rule(tmp_path):
    import pytest

    from server.repos import review_rule_repo

    conn = _db(tmp_path)
    with pytest.raises(ValueError):
        review_rule_repo.set_status(conn, 1, "proposed")
    assert review_rule_repo.set_status(conn, 999, "active") is None


def test_active_rules_source_is_repo_scoped_and_bounded(tmp_path):
    from server.context.review_rules_source import active_review_rules_source
    from server.repos import review_rule_repo

    conn = _db(tmp_path)
    api_id = repo_repo.add(conn, full_name="acme/api")
    web_id = repo_repo.add(conn, full_name="acme/web")
    api_rule = review_rule_repo.propose_rules(
        conn,
        api_id,
        [{"category": "style", "approved": 0, "edited": 0, "rejected": 3}],
    )[0]
    web_rule = review_rule_repo.propose_rules(
        conn,
        web_id,
        [{"category": "perf", "approved": 0, "edited": 0, "rejected": 3}],
    )[0]
    review_rule_repo.set_status(conn, api_rule["id"], "active")
    review_rule_repo.set_status(conn, web_rule["id"], "active")
    conn.close()

    req = ContextRequest(repo="acme/api", pr_number=7)
    text = active_review_rules_source(db_path=tmp_path / "rules.db")(req)
    assert "style" in text and "perf" not in text
    assert "승인한 리뷰 규칙" in text


def test_registry_injects_active_rules_only_when_feedback_context_is_enabled(tmp_path, monkeypatch):
    from server import config
    from server.context.registry import build_context_provider
    from server.repos import review_rule_repo

    conn = _db(tmp_path)
    rid = repo_repo.add(conn, full_name="acme/api")
    rule = review_rule_repo.propose_rules(
        conn,
        rid,
        [{"category": "style", "approved": 0, "edited": 0, "rejected": 3}],
    )[0]
    review_rule_repo.set_status(conn, rule["id"], "active")
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "rules.db"))

    enabled = build_context_provider({"context_feedback_on": 1}, {})
    disabled = build_context_provider({"context_feedback_on": 0}, {})
    assert any(p.name == "review_rules" for p in enabled.providers)
    assert not any(p.name == "review_rules" for p in disabled.providers)
    rendered = enabled.gather(req=ContextRequest(repo="acme/api", pr_number=7))
    assert "### review_rules" in rendered and "동작 영향" in rendered
