import asyncio

from server.poller import poll_loop, poll_once
from server.repos import repo_repo, pr_repo
from server.github.gh import PrInfo


def test_poll_once_upserts_new_prs(db):
    repo_repo.add(db, full_name="acme/api")
    fake_prs = [PrInfo(7, "t", "kim", "sha1", "main", "u", "open")]
    enqueued = []
    poll_once(
        db, list_prs=lambda repo: fake_prs, enqueue=lambda pr_id: enqueued.append(pr_id)
    )
    # PR upsert + head_sha != last_reviewed_sha → enqueue
    pid = pr_repo.get(db, 1)["id"]
    assert enqueued == [pid]


def test_poll_once_skips_already_reviewed(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="sha1",
        base_ref="main",
        url="u",
    )
    pr_repo.mark_reviewed(db, pid, "sha1")
    enqueued = []
    poll_once(
        db,
        list_prs=lambda repo: [PrInfo(7, "t", "a", "sha1", "main", "u", "open")],
        enqueue=lambda pr_id: enqueued.append(pr_id),
    )
    assert enqueued == []  # 같은 sha → skip


def test_poll_once_no_vendor_upserts_pr_but_skips_enqueue(db):
    # ★개정 (codex v6/v7 [MEDIUM]): 벤더 0개 레포도 PR은 발견·upsert(오버뷰 표시)
    # 하되 enqueue만 막는다(재감지 루프 차단). 벤더를 켜면 다음 폴링에 enqueue.
    rid = repo_repo.add(db, full_name="acme/api")
    repo_repo.update(db, rid, vendor_claude_on=0, vendor_codex_on=0)
    prs = [PrInfo(7, "t", "a", "sha1", "main", "u", "open")]
    enqueued = []
    poll_once(
        db, list_prs=lambda repo: prs, enqueue=lambda pr_id: enqueued.append(pr_id)
    )
    assert enqueued == []  # 벤더 0개 → enqueue 안 함
    assert pr_repo.get(db, 1)["head_sha"] == "sha1"  # PR은 upsert됨(오버뷰 노출)

    # 벤더 재활성화 → 같은 head_sha가 다음 폴링에 정상 enqueue
    repo_repo.update(db, rid, vendor_claude_on=1)
    poll_once(
        db, list_prs=lambda repo: prs, enqueue=lambda pr_id: enqueued.append(pr_id)
    )
    assert enqueued == [1]  # 재활성화 후 enqueue 성립


def test_poll_once_reenqueues_on_changed_sha(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pid = pr_repo.upsert(
        db,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="sha1",
        base_ref="main",
        url="u",
    )
    pr_repo.mark_reviewed(db, pid, "sha1")
    enqueued = []
    poll_once(
        db,
        list_prs=lambda repo: [PrInfo(7, "t", "a", "sha2", "main", "u", "open")],
        enqueue=lambda pr_id: enqueued.append(pr_id),
    )
    assert enqueued == [pid]  # head_sha 변경 → 재리뷰 enqueue
    assert pr_repo.get(db, pid)["head_sha"] == "sha2"


def test_poll_once_reconciles_dropped_pr_to_closed(db):
    repo_repo.add(db, full_name="acme/api")
    both = [
        PrInfo(7, "t", "a", "s7", "main", "u", "open"),
        PrInfo(8, "t", "a", "s8", "main", "u", "open"),
    ]
    poll_once(db, list_prs=lambda repo: both, enqueue=lambda pid: None)
    # PR #8이 병합돼 다음 폴에서 gh --state open 목록에서 사라짐
    poll_once(db, list_prs=lambda repo: [both[0]], enqueue=lambda pid: None)
    rows = {
        r["number"]: r["state"]
        for r in db.execute("SELECT number, state FROM pull_request").fetchall()
    }
    assert rows == {7: "open", 8: "closed"}  # 사라진 #8만 closed로 재조정


def test_poll_once_marks_dropped_closed_when_none_open(db):
    repo_repo.add(db, full_name="acme/api")
    poll_once(
        db,
        list_prs=lambda repo: [PrInfo(7, "t", "a", "s7", "main", "u", "open")],
        enqueue=lambda pid: None,
    )
    poll_once(db, list_prs=lambda repo: [], enqueue=lambda pid: None)  # 전부 닫힘
    assert pr_repo.get(db, 1)["state"] == "closed"


def test_poll_once_does_not_close_pr_inserted_during_poll(db):
    rid = repo_repo.add(db, full_name="acme/api")
    pr_repo.upsert(
        db,
        repo_id=rid,
        number=7,
        title="t",
        author="a",
        head_sha="s7",
        base_ref="main",
        url="u",
    )  # 기존 열린 #7

    def list_prs_inserting_6(repo):
        # 폴 도중 웹훅이 #6를 open으로 삽입(gh 스냅샷·prev_open 모두에 없음)
        pr_repo.upsert(
            db,
            repo_id=rid,
            number=6,
            title="t",
            author="a",
            head_sha="s6",
            base_ref="main",
            url="u",
        )
        return [PrInfo(7, "t", "a", "s7", "main", "u", "open")]

    poll_once(db, list_prs=list_prs_inserting_6, enqueue=lambda pid: None)
    states = {
        r["number"]: r["state"]
        for r in db.execute("SELECT number, state FROM pull_request").fetchall()
    }
    assert states == {6: "open", 7: "open"}  # 동시 삽입 #6는 오검-close되지 않음


def test_poll_once_skips_reconcile_when_list_truncated(db, monkeypatch):
    from server import config

    monkeypatch.setattr(config, "POLL_OPEN_PR_LIMIT", 1)
    rid = repo_repo.add(db, full_name="acme/api")
    pr_repo.upsert(
        db,
        repo_id=rid,
        number=8,
        title="t",
        author="a",
        head_sha="s8",
        base_ref="main",
        url="u",
    )  # 기존 열린 PR
    # 폴이 상한(1)만큼 반환 → 셋이 잘렸을 수 있어 재조정 skip(오검-close 방지)
    poll_once(
        db,
        list_prs=lambda repo: [PrInfo(7, "t", "a", "s7", "main", "u", "open")],
        enqueue=lambda pid: None,
    )
    assert pr_repo.get(db, 1)["state"] == "open"  # #8 유지


def test_poll_once_manual_discovers_pr_but_skips_enqueue(db):
    # ★개정: manual 레포도 PR을 발견·upsert(대시보드 노출→사람이 리뷰 버튼으로 트리거)
    # 하되 자동 enqueue만 막는다. (예전엔 레포 전체를 skip해 새 PR이 안 떠 수동 트리거 불가)
    rid = repo_repo.add(db, full_name="acme/api")
    repo_repo.update(db, rid, trigger_mode="manual")
    enqueued = []
    poll_once(
        db,
        list_prs=lambda repo: [PrInfo(7, "t", "a", "sha1", "main", "u", "open")],
        enqueue=lambda pr_id: enqueued.append(pr_id),
    )
    assert enqueued == []  # manual → 자동 리뷰 enqueue 안 함
    assert pr_repo.get(db, 1)["head_sha"] == "sha1"  # 그러나 PR은 발견·upsert됨


def test_poll_once_isolates_repo_failure(db):
    # 한 레포의 gh 실패가 뒤 레포 폴링을 막지 않는다(per-repo 격리).
    repo_repo.add(db, full_name="acme/bad")
    repo_repo.add(db, full_name="acme/good")
    enqueued = []

    def list_prs(full_name):
        if full_name == "acme/bad":
            raise RuntimeError("gh boom")
        return [PrInfo(7, "t", "a", "sha1", "main", "u", "open")]

    poll_once(db, list_prs=list_prs, enqueue=lambda pid: enqueued.append(pid))
    assert enqueued == [1]  # good 레포 PR은 정상 발견·enqueue
    polled = {
        r["full_name"]: r["last_polled_at"]
        for r in db.execute("SELECT full_name, last_polled_at FROM repo").fetchall()
    }
    assert polled["acme/good"] is not None  # good은 폴링 완료 기록
    assert polled["acme/bad"] is None  # bad는 실패로 미기록(다음 틱 재시도)


def test_poll_loop_survives_tick_error(tmp_path, monkeypatch):
    stop = asyncio.Event()
    calls = []

    def fake_poll_once(conn, *, list_prs, enqueue):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("tick boom")  # 첫 틱은 폭발
        stop.set()

    monkeypatch.setattr("server.poller.poll_once", fake_poll_once)
    asyncio.run(poll_loop(tmp_path / "p.db", interval_sec=0.01, stop_event=stop))
    assert len(calls) >= 2  # 첫 에러 후에도 루프 생존


def test_poll_loop_reads_interval_from_settings(tmp_path, monkeypatch):
    from server.db import connect, init_schema
    from server.repos import settings_repo

    db = tmp_path / "p.db"
    conn = connect(db)
    init_schema(conn)
    settings_repo.update(conn, default_poll_interval=123)
    conn.close()

    stop = asyncio.Event()
    seen = []

    def fake_poll_once(conn, *, list_prs, enqueue):
        pass  # 성공 틱 → 설정값이 읽힘

    async def fake_wait_for(aw, timeout):
        seen.append(timeout)
        stop.set()
        aw.close()  # stop_event.wait() 코루틴 정리
        return None

    monkeypatch.setattr("server.poller.poll_once", fake_poll_once)
    monkeypatch.setattr("server.poller.asyncio.wait_for", fake_wait_for)
    asyncio.run(poll_loop(db, interval_sec=999, stop_event=stop))
    assert seen == [123]  # interval_sec 폴백(999) 아니라 설정값(123) 사용


def test_poll_once_skips_draft_pr_by_default(db):
    repo_repo.add(db, full_name="acme/api")
    prs = [
        PrInfo(7, "t", "a", "s7", "main", "u", "open", is_draft=True),
        PrInfo(8, "t", "a", "s8", "main", "u", "open"),
    ]
    enqueued = []
    poll_once(
        db, list_prs=lambda repo: prs, enqueue=lambda pr_id: enqueued.append(pr_id)
    )
    drafted = db.execute(
        "SELECT id FROM pull_request WHERE number=7"
    ).fetchone()  # draft도 upsert는 됨(오버뷰 노출)
    ready = db.execute("SELECT id FROM pull_request WHERE number=8").fetchone()
    assert drafted is not None and enqueued == [ready["id"]]

    # draft가 ready로 전환되면 다음 폴링에 enqueue
    prs[0] = PrInfo(7, "t", "a", "s7", "main", "u", "open", is_draft=False)
    poll_once(
        db, list_prs=lambda repo: prs, enqueue=lambda pr_id: enqueued.append(pr_id)
    )
    assert drafted["id"] in enqueued


def test_poll_once_reviews_draft_when_skip_off_globally(db):
    from server.repos import settings_repo

    settings_repo.update(db, skip_draft_on=0)
    repo_repo.add(db, full_name="acme/api")
    enqueued = []
    poll_once(
        db,
        list_prs=lambda repo: [
            PrInfo(7, "t", "a", "s7", "main", "u", "open", is_draft=True)
        ],
        enqueue=lambda pr_id: enqueued.append(pr_id),
    )
    assert enqueued == [1]


def test_poll_once_repo_override_beats_global_skip(db):
    from server.repos import settings_repo

    settings_repo.update(db, skip_draft_on=0)
    rid = repo_repo.add(db, full_name="acme/api")
    repo_repo.update(db, rid, skip_draft_on=1)  # 레포가 skip을 다시 켬
    enqueued = []
    poll_once(
        db,
        list_prs=lambda repo: [
            PrInfo(7, "t", "a", "s7", "main", "u", "open", is_draft=True)
        ],
        enqueue=lambda pr_id: enqueued.append(pr_id),
    )
    assert enqueued == []
