import pytest

from server import config
from server.db import connect, init_schema


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch):
    # 테스트 중 osascript 알림이 실제로 뜨지 않게 전역 차단.
    monkeypatch.setattr(config, "NOTIFY_ON_DONE", False)


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    yield conn
    conn.close()
