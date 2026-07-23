from server.db import connect, init_schema
from server.repos import job_repo, pr_repo, process_repo, repo_repo, review_repo


def _seed(conn):
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn, repo_id=rid, number=1, title="t", author="a", head_sha="s",
        base_ref="main", url="u",
    )
    return pid


def test_active_process_work_is_not_recovered(tmp_path):
    conn = connect(tmp_path / "lease.db")
    init_schema(conn)
    process_repo.register(conn, "alive", ttl_seconds=60)
    pid = _seed(conn)
    jid = job_repo.enqueue(conn, pr_id=pid, head_sha="s", trigger="auto")
    job_repo.claim_next(conn, worker_id="w1", owner_process_id="alive")
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="auto", effort="medium",
        owner_process_id="alive",
    )

    recovered = process_repo.recover_expired(conn)

    assert recovered == {"jobs": 0, "runs": 0, "wiki": 0}
    assert conn.execute("SELECT status FROM review_job WHERE id=?", (jid,)).fetchone()[0] == "running"
    assert review_repo.get_run(conn, run_id)["status"] == "running"


def test_expired_process_work_is_recovered_and_old_owner_is_fenced(tmp_path):
    conn = connect(tmp_path / "expired.db")
    init_schema(conn)
    process_repo.register(conn, "dead", ttl_seconds=60)
    pid = _seed(conn)
    jid = job_repo.enqueue(conn, pr_id=pid, head_sha="s", trigger="auto")
    job_repo.claim_next(conn, worker_id="w1", owner_process_id="dead")
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="auto", effort="medium",
        owner_process_id="dead",
    )
    conn.execute("UPDATE process_lease SET expires_at=datetime('now', '-1 second')")
    conn.commit()

    recovered = process_repo.recover_expired(conn)

    assert recovered["jobs"] == 1 and recovered["runs"] == 1
    assert conn.execute("SELECT status FROM review_job WHERE id=?", (jid,)).fetchone()[0] == "queued"
    try:
        job_repo.mark_done(conn, jid, run_id, owner_process_id="dead")
    except job_repo.LeaseLostError:
        pass
    else:
        raise AssertionError("expired owner must be fenced")
