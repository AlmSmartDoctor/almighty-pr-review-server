from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from server import config
from server.api import app, get_conn
from server.db import connect, init_schema
from server.pagination import decode_cursor, encode_cursor


def _client(tmp_path):
    conn = connect(tmp_path / "pagination.db")
    init_schema(conn)
    app.dependency_overrides[get_conn] = lambda: conn
    return TestClient(app), conn


def _repo_pr(conn, number: int, *, created_at: str):
    row = conn.execute("SELECT id FROM repo WHERE full_name='acme/api'").fetchone()
    repo_id = row["id"] if row else conn.execute(
        "INSERT INTO repo(full_name) VALUES ('acme/api')"
    ).lastrowid
    pr_id = conn.execute(
        """INSERT INTO pull_request(repo_id,number,title,head_sha,state,created_at)
           VALUES (?,?,?,'head','open',?)""",
        (repo_id, number, f"PR {number}", created_at),
    ).lastrowid
    conn.commit()
    return pr_id


def test_cursor_round_trip_tamper_and_binding(monkeypatch):
    monkeypatch.setattr(config, "PAGINATION_CURSOR_SECRET", "s" * 32)
    token = encode_cursor(
        resource="pr-runs", parent=7, snapshot_max_id=99, position=[42]
    )
    assert decode_cursor(token, resource="pr-runs", parent=7) == {
        "v": 1, "resource": "pr-runs", "parent": 7,
        "snapshot": 99, "position": [42], "metadata": {},
    }
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_cursor(token[:-1] + ("A" if token[-1] != "A" else "B"), resource="pr-runs", parent=7)
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_cursor(token, resource="run-findings", parent=7)
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_cursor(token, resource="pr-runs", parent=8)


def test_overview_pages_are_bounded_and_snapshot_excludes_later_insert(tmp_path):
    client, conn = _client(tmp_path)
    now = datetime.now(timezone.utc)
    ids = [
        _repo_pr(conn, number, created_at=(now - timedelta(minutes=number)).isoformat())
        for number in (1, 2, 3)
    ]
    first = client.get("/api/overview", params={"limit": 2}).json()
    assert len(first["items"]) == 2
    assert first["pagination"]["has_more"] is True
    later = _repo_pr(conn, 9, created_at=(now + timedelta(minutes=1)).isoformat())
    second = client.get(
        "/api/overview",
        params={"limit": 2, "cursor": first["pagination"]["next_cursor"]},
    ).json()
    returned = [item["id"] for item in first["items"] + second["items"]]
    assert returned == ids
    assert later not in returned
    assert second["pagination"]["has_more"] is False
    app.dependency_overrides.clear()
    conn.close()


def test_overview_direct_lookup_and_cursor_validation(tmp_path):
    client, conn = _client(tmp_path)
    pr_id = _repo_pr(conn, 1, created_at="2026-01-01T00:00:00Z")
    direct = client.get("/api/overview", params={"pr_id": pr_id}).json()
    assert [item["id"] for item in direct["items"]] == [pr_id]
    assert direct["pagination"]["has_more"] is False
    assert client.get(
        "/api/overview", params={"pr_id": pr_id, "cursor": "bad"}
    ).status_code == 400
    assert client.get("/api/overview", params={"limit": 101}).status_code == 422
    app.dependency_overrides.clear()
    conn.close()


def test_run_history_cursor_is_parent_bound_and_snapshot_stable(tmp_path):
    client, conn = _client(tmp_path)
    pr_id = _repo_pr(conn, 1, created_at="2026-01-01T00:00:00Z")
    other = _repo_pr(conn, 2, created_at="2026-01-02T00:00:00Z")
    run_ids = [
        conn.execute(
            "INSERT INTO review_run(pr_id,head_sha,status) VALUES (?,'head','done')",
            (pr_id,),
        ).lastrowid
        for _ in range(3)
    ]
    conn.commit()
    first = client.get(f"/api/prs/{pr_id}/runs", params={"limit": 2}).json()
    cursor = first["pagination"]["next_cursor"]
    inserted = conn.execute(
        "INSERT INTO review_run(pr_id,head_sha,status) VALUES (?,'head','done')",
        (pr_id,),
    ).lastrowid
    conn.commit()
    second = client.get(
        f"/api/prs/{pr_id}/runs", params={"limit": 2, "cursor": cursor}
    ).json()
    returned = [item["id"] for item in first["items"] + second["items"]]
    assert returned == list(reversed(run_ids))
    assert inserted not in returned
    assert client.get(
        f"/api/prs/{other}/runs", params={"cursor": cursor}
    ).status_code == 400
    app.dependency_overrides.clear()
    conn.close()


def test_page_query_plans_seek_indexes_without_temp_sort(db):
    pr_id = _repo_pr(db, 1, created_at="2026-01-01T00:00:00Z")
    run_id = db.execute(
        "INSERT INTO review_run(pr_id,head_sha,status) VALUES (?,'head','done')",
        (pr_id,),
    ).lastrowid
    db.execute(
        """INSERT INTO finding(run_id,vendor,file,severity,claim,confidence)
           VALUES (?,'codex','a.py','high','claim',0.5)""",
        (run_id,),
    )
    db.commit()
    plans = [
        db.execute(
            """EXPLAIN QUERY PLAN SELECT p.id,r.full_name
               FROM pull_request p INDEXED BY idx_pull_request_overview_page
               JOIN repo r ON r.id=p.repo_id
               WHERE p.state='open' AND p.id<=?
                 AND (p.overview_sort_at,p.id)<(?,?)
               ORDER BY p.overview_sort_at DESC,p.id DESC LIMIT ?""",
            (999, "9999-01-01 00:00:00", 999, 10),
        ).fetchall(),
        db.execute(
            """EXPLAIN QUERY PLAN SELECT id FROM review_run
               INDEXED BY idx_review_run_page
               WHERE pr_id=? AND id<=? ORDER BY id DESC LIMIT ?""",
            (pr_id, 999, 10),
        ).fetchall(),
        db.execute(
            """EXPLAIN QUERY PLAN SELECT id FROM finding
               INDEXED BY idx_finding_run_page_v2 WHERE run_id=? AND id<=?
               ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2 ELSE 3 END,
                        CASE consensus WHEN 'consensus' THEN 0 ELSE 1 END,
                        -COALESCE(confidence,-1),COALESCE(file,''),id LIMIT ?""",
            (run_id, 999, 10),
        ).fetchall(),
    ]
    for plan in plans:
        detail = " ".join(row[3] for row in plan)
        assert "SEARCH" in detail
        assert "TEMP B-TREE" not in detail


def test_findings_priority_pages_and_authoritative_summary(tmp_path):
    client, conn = _client(tmp_path)
    pr_id = _repo_pr(conn, 1, created_at="2026-01-01T00:00:00Z")
    run_id = conn.execute(
        "INSERT INTO review_run(pr_id,head_sha,status) VALUES (?,'head','done')",
        (pr_id,),
    ).lastrowid
    rows = [
        ("low", None, 0.1, "z.py", "pending", 1),
        ("high", "consensus", 0.5, "b.py", "approved", 1),
        ("high", None, 0.9, "a.py", "dismissed", 0),
    ]
    ids = []
    for severity, consensus, confidence, file, status, eligible in rows:
        ids.append(conn.execute(
            """INSERT INTO finding(
                   run_id,vendor,file,line,severity,claim,status,consensus,
                   confidence,posting_eligible
               ) VALUES (?,'codex',?,1,?,'claim',?,?,?,?)""",
            (run_id, file, severity, status, consensus, confidence, eligible),
        ).lastrowid)
    conn.commit()
    first = client.get(
        f"/api/runs/{run_id}/findings", params={"limit": 2}
    ).json()
    assert [item["id"] for item in first["items"]] == [ids[1], ids[2]]
    assert first["summary"] == {
        "total_count": 3,
        "status_counts": {"approved": 1, "dismissed": 1, "pending": 1},
        "postable_count": 1,
    }
    later = conn.execute(
        """INSERT INTO finding(run_id,vendor,file,line,severity,claim,status)
           VALUES (?,'codex','new.py',1,'critical','new','approved')""",
        (run_id,),
    ).lastrowid
    conn.commit()
    second = client.get(
        f"/api/runs/{run_id}/findings",
        params={"limit": 2, "cursor": first["pagination"]["next_cursor"]},
    ).json()
    assert [item["id"] for item in second["items"]] == [ids[0]]
    assert later not in [item["id"] for item in second["items"]]
    assert second["summary"] == first["summary"]
    conn.execute("UPDATE finding SET status='approved' WHERE id=?", (ids[0],))
    conn.execute("DELETE FROM finding WHERE id=?", (ids[2],))
    conn.commit()
    refreshed = client.get(f"/api/runs/{run_id}/findings").json()["summary"]
    assert refreshed == {
        "total_count": 3,
        "status_counts": {"approved": 3},
        "postable_count": 3,
    }
    app.dependency_overrides.clear()
    conn.close()
