from server.repos import (
    repo_repo,
    pr_repo,
    finding_repo,
    settings_repo,
    review_repo,
    prescreen_repo,
)


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


def test_list_for_run_orders_by_severity_consensus_confidence(db):
    # 위험도 rank(critical<high<medium<low) → 합의 우선 → 신뢰도 내림차순.
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=9,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = db.execute(
        "INSERT INTO review_run (pr_id, head_sha) VALUES (?, ?)", (pid, "s")
    ).lastrowid

    def _add(sev, conf, consensus="single", file="z.py"):
        return finding_repo.add(
            db,
            run_id=run_id,
            vendor="claude",
            file=file,
            line=1,
            severity=sev,
            category="bug",
            claim="c",
            rationale="r",
            confidence=conf,
            consensus=consensus,
        )

    # 삽입 순서를 정렬 결과와 어긋나게 섞는다.
    f_low = _add("low", 0.9)
    f_med = _add("medium", 0.2)  # TEXT 정렬이면 low보다 뒤로 갔던 버그 케이스
    f_high_single = _add("high", 0.9)
    f_high_consensus = _add("high", 0.3, consensus="consensus")

    order = [r["id"] for r in finding_repo.list_for_run(db, run_id)]
    # high 합의(저신뢰라도 합의 우선) → high 단일 → medium → low
    assert order == [f_high_consensus, f_high_single, f_med, f_low]


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


def _seed_one_finding(db, number):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=number,
        title="t",
        author="a",
        head_sha="s",
        base_ref="main",
        url="u",
    )
    run_id = db.execute(
        "INSERT INTO review_run (pr_id, head_sha) VALUES (?, ?)", (pid, "s")
    ).lastrowid
    return finding_repo.add(
        db,
        run_id=run_id,
        vendor="claude",
        file="a.py",
        line=1,
        severity="high",
        category="bug",
        claim="c",
        rationale="r",
        confidence=0.8,
    )


def test_set_status_records_decision_audit(db):
    fid = _seed_one_finding(db, 3)
    finding_repo.set_status(db, fid, "approved")
    finding_repo.set_status(db, fid, "approved")  # 무변경 → 감사 미기록
    finding_repo.set_status(db, fid, "posted")
    rows = db.execute(
        "SELECT from_status, to_status FROM finding_decision "
        "WHERE finding_id=? ORDER BY id",
        (fid,),
    ).fetchall()
    assert [(r["from_status"], r["to_status"]) for r in rows] == [
        ("pending", "approved"),
        ("approved", "posted"),
    ]  # 재-approved(무변경)은 스킵, from_status 정확


def test_set_status_missing_finding_is_noop(db):
    # 존재하지 않는 finding → FK 위반 없이 감사 미기록(prev None 가드)
    finding_repo.set_status(db, 9999, "approved")
    assert db.execute("SELECT COUNT(*) c FROM finding_decision").fetchone()["c"] == 0


def test_set_status_edited_branch_records_decision_audit(db):
    # edited_text 분기(사람 수정)도 감사 행 1개를 남겨야 함 — INSERT가 if/else 밖에 있음을 고정
    fid = _seed_one_finding(db, 4)
    finding_repo.set_status(db, fid, "edited", edited_text="human text")
    rows = db.execute(
        "SELECT from_status, to_status FROM finding_decision "
        "WHERE finding_id=? ORDER BY id",
        (fid,),
    ).fetchall()
    assert [(r["from_status"], r["to_status"]) for r in rows] == [("pending", "edited")]


def test_settings_context_toggles_roundtrip(db):
    settings_repo.update(
        db,
        context_static_on=1,
        context_jira_on=1,
        context_db_schema_on=1,
        context_graphify_on=1,
        context_feedback_on=1,
    )
    s = settings_repo.get(db)
    assert (
        s["context_static_on"],
        s["context_jira_on"],
        s["context_db_schema_on"],
        s["context_graphify_on"],
        s["context_feedback_on"],
    ) == (1, 1, 1, 1, 1)


def test_repo_context_settings_roundtrip(db):
    rid = repo_repo.add(db, full_name="acme/api")
    assert repo_repo.get(db, rid)["context_static_on"] is None  # 기본 NULL = 상속
    repo_repo.update(
        db,
        rid,
        context_static_on=1,
        context_feedback_on=1,
        static_context_path="/x/ctx.md",
        jira_project_keys="PROJ,ABC",
        db_schema_path="db/structure.sql",
        graphify_path="docs/PROJECT.md",
    )
    r = repo_repo.get(db, rid)
    assert r["context_static_on"] == 1
    assert r["context_feedback_on"] == 1
    assert r["static_context_path"] == "/x/ctx.md"
    assert r["jira_project_keys"] == "PROJ,ABC"
    assert r["db_schema_path"] == "db/structure.sql"
    assert r["graphify_path"] == "docs/PROJECT.md"


def test_verify_singles_toggle_roundtrip(db):
    settings_repo.update(db, verify_singles_on=1)
    assert settings_repo.get(db)["verify_singles_on"] == 1
    rid = repo_repo.add(db, full_name="acme/api")
    assert repo_repo.get(db, rid)["verify_singles_on"] is None  # 기본 NULL = 상속
    repo_repo.update(db, rid, verify_singles_on=0)
    assert repo_repo.get(db, rid)["verify_singles_on"] == 0


def test_incremental_toggle_roundtrip(db):
    settings_repo.update(db, incremental_review_on=1)
    assert settings_repo.get(db)["incremental_review_on"] == 1
    rid = repo_repo.add(db, full_name="acme/api")
    assert repo_repo.get(db, rid)["incremental_review_on"] is None  # NULL=상속
    repo_repo.update(db, rid, incremental_review_on=0)
    assert repo_repo.get(db, rid)["incremental_review_on"] == 0


def test_last_done_head_sha_only_counts_done_runs(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=8,
        title="t",
        author="a",
        head_sha="s1",
        base_ref="main",
        url="u",
    )
    assert review_repo.last_done_head_sha(db, pid) is None  # 아직 done 없음
    r1 = review_repo.create_run(
        db, pr_id=pid, head_sha="s1", trigger="manual", effort="medium"
    )
    review_repo.finish_run(db, r1, "done")
    r2 = review_repo.create_run(
        db, pr_id=pid, head_sha="s2", trigger="auto", effort="medium"
    )
    review_repo.finish_run(db, r2, "canceled")  # skip → 기준선 아님
    assert review_repo.last_done_head_sha(db, pid) == "s1"
    r3 = review_repo.create_run(
        db, pr_id=pid, head_sha="s3", trigger="auto", effort="medium"
    )
    review_repo.finish_run(db, r3, "done")
    assert review_repo.last_done_head_sha(db, pid) == "s3"


def test_set_base_sha_roundtrip(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=8,
        title="t",
        author="a",
        head_sha="s9",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        db, pr_id=pid, head_sha="s9", trigger="manual", effort="medium"
    )
    assert review_repo.get_run(db, run_id)["base_sha"] is None
    review_repo.set_base_sha(db, run_id, "prevsha")
    assert review_repo.get_run(db, run_id)["base_sha"] == "prevsha"


def test_prescreen_find_reusable_matches_diff_hash_and_model(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=8,
        title="t",
        author="a",
        head_sha="s1",
        base_ref="main",
        url="u",
    )
    prescreen_repo.add(
        db,
        pr_id=pid,
        head_sha="s1",
        model="haiku",
        complexity="complex",
        score=0.9,
        reason="r",
        duration_ms=0,
        decided="review",
        diff_hash="h1",
    )
    assert (
        prescreen_repo.find_reusable(db, pid, "h1", "haiku")["complexity"] == "complex"
    )
    assert prescreen_repo.find_reusable(db, pid, "h2", "haiku") is None  # diff 다름
    assert prescreen_repo.find_reusable(db, pid, "h1", "sonnet") is None  # model 다름
    assert prescreen_repo.find_reusable(db, 999, "h1", "haiku") is None  # 다른 PR


def test_list_vendor_results_computes_live_elapsed_for_running(db):
    """running 벤더는 저장된 duration이 없으므로 서버가 경과시간을 실시간 계산해 반환."""
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=8,
        title="t",
        author="a",
        head_sha="s1",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        db, pr_id=pid, head_sha="s1", trigger="manual", effort="medium"
    )
    vr = review_repo.add_vendor_result(
        db, run_id=run_id, vendor="claude", status="running"
    )
    db.execute(
        "UPDATE vendor_result SET started_at=datetime('now','-5 seconds') WHERE id=?",
        (vr,),
    )
    db.commit()

    row = review_repo.list_vendor_results(db, run_id)[0]
    assert row["status"] == "running"
    assert row["duration_ms"] >= 4000  # ~5초 경과를 실시간 계산(초 granularity 여유)


def test_finding_persists_verify_columns(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=9,
        title="t",
        author="a",
        head_sha="s9",
        base_ref="main",
        url="u",
    )
    run_id = review_repo.create_run(
        db, pr_id=pid, head_sha="s9", trigger="manual", effort="medium"
    )
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
        confidence=0.4,
        verify_status="refuted",
        verify_rationale="오탐",
    )
    f = finding_repo.get(db, fid)
    assert f["verify_status"] == "refuted"
    assert f["verify_rationale"] == "오탐"
