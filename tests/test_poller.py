from server.poller import poll_once
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
