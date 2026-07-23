import os
import time

from server import config
from server.api import _diagnostic_cleanup_coro
from server.db import connect, init_schema
from server.repos import finding_repo, pr_repo, repo_repo, review_repo
from server.retention import (
    cleanup,
    cleanup_context_payloads,
    cleanup_raw_diagnostics,
)


def test_retention_is_disabled_by_default(tmp_path):
    conn = connect(tmp_path / "r.db")
    init_schema(conn)
    assert cleanup(conn, retention_days=0, raw_dir=tmp_path) == {"prs": 0, "raw_files": 0}


def test_diagnostic_cleanup_scheduler_is_default_off(monkeypatch):
    monkeypatch.setattr(config, "DIAGNOSTIC_CLEANUP_ENABLED", False)
    assert _diagnostic_cleanup_coro(object()) is None


def test_diagnostic_cleanup_scheduler_requires_explicit_opt_in(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(config, "DIAGNOSTIC_CLEANUP_ENABLED", True)
    monkeypatch.setattr(
        "server.retention.diagnostic_cleanup_loop",
        lambda *args, **kwargs: sentinel,
    )
    assert _diagnostic_cleanup_coro(object()) is sentinel


def test_retention_deletes_only_old_closed_pr_and_confined_raw(tmp_path):
    conn = connect(tmp_path / "r.db")
    init_schema(conn)
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn, repo_id=rid, number=1, title="t", author="a", head_sha="s",
        base_ref="main", url="u", state="closed",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    review_repo.finish_run(conn, run_id, "done")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw = raw_dir / "vendor.txt"
    raw.write_text("x")
    review_repo.add_vendor_result(
        conn, run_id=run_id, vendor="claude", status="done", raw_path=str(raw)
    )
    finding_repo.add(
        conn, run_id=run_id, vendor="claude", file="a.py", line=1,
        severity="high", category="bug", claim="c", rationale="r", confidence=.9,
    )
    conn.execute(
        "UPDATE pull_request SET updated_at=datetime('now', '-40 days') WHERE id=?",
        (pid,),
    )
    conn.commit()

    result = cleanup(conn, retention_days=30, raw_dir=raw_dir)

    assert result == {"prs": 1, "raw_files": 1}
    assert pr_repo.get(conn, pid) is None
    assert not raw.exists()


def test_diagnostic_retention_clears_old_raw_without_deleting_review(tmp_path):
    conn = connect(tmp_path / "diagnostic.db")
    init_schema(conn)
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn, repo_id=rid, number=2, title="t", author="a", head_sha="s",
        base_ref="main", url="u", state="open",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, head_sha="s", trigger="manual", effort="medium"
    )
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw = raw_dir / "legacy.txt"
    raw.write_text("sensitive")
    old = time.time() - 8 * 86_400
    os.utime(raw, (old, old))
    vr_id = review_repo.add_vendor_result(
        conn, run_id=run_id, vendor="claude", status="done", raw_path=str(raw)
    )

    result = cleanup_raw_diagnostics(conn, retention_days=7, raw_dir=raw_dir)

    assert result == {"raw_files": 1, "paths_cleared": 1}
    assert not raw.exists()
    assert review_repo.get_run(conn, run_id) is not None
    assert conn.execute(
        "SELECT raw_path FROM vendor_result WHERE id=?", (vr_id,)
    ).fetchone()[0] is None


def test_context_payload_retention_clears_text_but_keeps_manifest(tmp_path):
    conn = connect(tmp_path / "context.db")
    init_schema(conn)
    rid = repo_repo.add(conn, full_name="acme/api")
    pid = pr_repo.upsert(
        conn, repo_id=rid, number=9, title="t", author="a",
        head_sha="sha", base_ref="main", url="u",
    )
    run_id = review_repo.create_run(
        conn, pr_id=pid, trigger="manual", head_sha="sha", effort="medium"
    )
    review_repo.set_context(
        conn, run_id, text="sensitive context", meta={"manifest": ["content-free"]}
    )
    review_repo.set_context_chunks(
        conn,
        run_id,
        chunks=[{
            "chunk_hash": "a" * 64,
            "context_hash": "b" * 64,
            "text": "sensitive chunk",
            "manifest": [{"source": "jira", "selected": True}],
        }],
    )
    conn.execute(
        "UPDATE review_run SET status='done', finished_at=datetime('now','-8 days') WHERE id=?",
        (run_id,),
    )
    conn.commit()

    result = cleanup_context_payloads(conn, retention_days=7)
    run = review_repo.get_run(conn, run_id)

    assert result == {"runs_cleared": 1}
    assert run["context_text"] is None and run["context_chunks"] is None
    assert "manifest" in run["context_meta"]


def test_diagnostic_retention_keeps_recent_and_outside_symlink(tmp_path):
    conn = connect(tmp_path / "safe.db")
    init_schema(conn)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    recent = raw_dir / "recent.txt"
    recent.write_text("x")
    outside = tmp_path / "outside.txt"
    outside.write_text("do not delete")
    link = raw_dir / "escape.txt"
    link.symlink_to(outside)

    cleanup_raw_diagnostics(conn, retention_days=7, raw_dir=raw_dir)

    assert recent.exists()
    assert outside.exists()
    assert link.exists()


def test_retention_sweeps_old_unreferenced_raw_after_prior_crash(tmp_path):
    conn = connect(tmp_path / "orphan.db")
    init_schema(conn)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    orphan = raw_dir / "orphan.txt"
    orphan.write_text("x")
    old = time.time() - 40 * 86_400
    os.utime(orphan, (old, old))

    result = cleanup(conn, retention_days=30, raw_dir=raw_dir)

    assert result["raw_files"] == 1
    assert not orphan.exists()
