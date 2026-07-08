import asyncio

from server import api, config
from server.db import connect


def test_lifespan_initializes_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "life.db")
    monkeypatch.setattr(api, "_initialized", False)

    async def drive():
        async with api.lifespan(api.app):
            conn = connect(config.DB_PATH)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='review_job'"
            ).fetchone()
            conn.close()
            assert row is not None  # schema created at startup, before loops touch DB

    asyncio.run(drive())
