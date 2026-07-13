from server.repos import repo_repo, pr_repo, finding_repo, settings_repo


def test_add_and_get_repo(db):
    rid = repo_repo.add(db, full_name="acme/api")
    row = repo_repo.get(db, rid)
    assert row["full_name"] == "acme/api"
    assert row["vendor_claude_on"] == 1


def test_upsert_pr_updates_head_sha(db):
    rid = repo_repo.add(db, full_name="acme/api")
    p1 = pr_repo.upsert(
        db,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="aaa",
        base_ref="main",
        url="u",
    )
    p2 = pr_repo.upsert(
        db,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="bbb",
        base_ref="main",
        url="u",
    )
    assert p1 == p2  # 같은 (repo, number) → 같은 id
    assert pr_repo.get(db, p1)["head_sha"] == "bbb"


def test_upsert_pr_stores_and_updates_created_at(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="aaa",
        base_ref="main",
        url="u",
        created_at="2026-07-07T11:22:33Z",
    )
    assert pr_repo.get(db, pid)["created_at"] == "2026-07-07T11:22:33Z"

    pr_repo.upsert(
        db,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="bbb",
        base_ref="main",
        url="u",
        created_at="2026-07-08T11:22:33Z",
    )
    assert pr_repo.get(db, pid)["created_at"] == "2026-07-08T11:22:33Z"


def test_upsert_pr_stores_head_ref_and_body(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="aaa",
        base_ref="main",
        url="u",
        head_ref="feature/PROJ-1",
        body="Closes PROJ-1",
    )
    row = pr_repo.get(db, pid)
    assert row["head_ref"] == "feature/PROJ-1"
    assert row["body"] == "Closes PROJ-1"


def test_upsert_pr_without_head_ref_and_body_defaults_empty(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=8,
        title="t",
        author="a",
        head_sha="aaa",
        base_ref="main",
        url="u",
    )
    row = pr_repo.get(db, pid)
    assert row["head_ref"] == ""
    assert row["body"] == ""


def test_finding_status_transition(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=1,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = db.execute(
        "INSERT INTO review_run (pr_id, head_sha) VALUES (?, ?)", (pid, "s")
    ).lastrowid
    fid = finding_repo.add(
        db,
        run_id=run_id,
        vendor="claude",
        file="a.py",
        line=3,
        severity="high",
        category="bug",
        claim="c",
        rationale="r",
        confidence=0.8,
    )
    finding_repo.set_status(db, fid, "approved")
    assert finding_repo.get(db, fid)["status"] == "approved"


def test_settings_singleton_update(db):
    settings_repo.update(db, concurrency_limit=4)
    assert settings_repo.get(db)["concurrency_limit"] == 4


def test_set_context_persists_text_and_meta(db):
    import json
    from server.repos import review_repo

    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=20,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        db, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    review_repo.set_context(
        db,
        run_id,
        text="ctx body",
        meta={"sources": [{"provider": "static", "status": "ok"}]},
    )
    run = review_repo.get_run(db, run_id)
    assert run["context_text"] == "ctx body"
    assert json.loads(run["context_meta"])["sources"][0]["provider"] == "static"


def test_set_status_preserves_edited_text(db):
    from server.repos import repo_repo, pr_repo, finding_repo

    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=2,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = db.execute(
        "INSERT INTO review_run (pr_id, head_sha) VALUES (?, ?)", (pid, "s")
    ).lastrowid
    fid = finding_repo.add(
        db,
        run_id=run_id,
        vendor="claude",
        file="a.py",
        line=1,
        severity="high",
        category="bug",
        claim="c",
        rationale="r",
        confidence=0.5,
    )
    finding_repo.set_status(db, fid, "edited", edited_text="human text")
    finding_repo.set_status(db, fid, "posted")  # status-only must NOT wipe edited_text
    row = finding_repo.get(db, fid)
    assert row["status"] == "posted"
    assert row["edited_text"] == "human text"


def test_settings_context_toggles_roundtrip(db):
    settings_repo.update(
        db,
        context_static_on=1,
        context_jira_on=1,
        context_db_schema_on=1,
        context_graphify_on=1,
    )
    s = settings_repo.get(db)
    assert (
        s["context_static_on"],
        s["context_jira_on"],
        s["context_db_schema_on"],
        s["context_graphify_on"],
    ) == (1, 1, 1, 1)


def test_repo_context_settings_roundtrip(db):
    rid = repo_repo.add(db, full_name="acme/api")
    assert repo_repo.get(db, rid)["context_static_on"] is None  # 기본 NULL = 상속
    repo_repo.update(
        db,
        rid,
        context_static_on=1,
        static_context_path="/x/ctx.md",
        jira_project_keys="PROJ,ABC",
    )
    r = repo_repo.get(db, rid)
    assert r["context_static_on"] == 1
    assert r["static_context_path"] == "/x/ctx.md"
    assert r["jira_project_keys"] == "PROJ,ABC"
