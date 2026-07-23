import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from server import config
from server.api import BackgroundShutdownError, _shutdown_background_tasks, app


def test_shutdown_cancels_running_review_and_wiki_tasks_after_grace(monkeypatch):
    monkeypatch.setattr(config, "BACKGROUND_SHUTDOWN_GRACE_SEC", 0.01)
    cleaned = []

    async def running(name):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(name)

    async def scenario():
        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(running("review"), name="worker-review"),
            asyncio.create_task(running("wiki"), name="worker-wiki"),
        ]
        await asyncio.sleep(0)
        results = await _shutdown_background_tasks(tasks, stop)
        assert stop.is_set()
        assert all(task.done() for task in tasks)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)

    asyncio.run(scenario())
    assert sorted(cleaned) == ["review", "wiki"]


def test_shutdown_deadline_does_not_release_a_cancellation_suppressing_task(
    monkeypatch
):
    monkeypatch.setattr(config, "BACKGROUND_SHUTDOWN_GRACE_SEC", 0.01)
    monkeypatch.setattr(config, "BACKGROUND_CLEANUP_TIMEOUT_SEC", 0.01)

    async def scenario():
        stop = asyncio.Event()

        async def stubborn():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                deadline = time.monotonic() + 0.1
                while time.monotonic() < deadline:
                    try:
                        await asyncio.sleep(deadline - time.monotonic())
                    except asyncio.CancelledError:
                        continue

        task = asyncio.create_task(stubborn(), name="stubborn-worker")
        await asyncio.sleep(0)
        started = time.monotonic()
        with pytest.raises(BackgroundShutdownError, match="stubborn-worker"):
            await _shutdown_background_tasks([task], stop)
        assert time.monotonic() - started < 0.08
        assert not task.done()
        await task

    asyncio.run(scenario())


def test_health_ok():
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["admin_auth_required"] is False
