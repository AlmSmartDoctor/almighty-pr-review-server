import pytest

from server.db import connect, init_schema


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    yield conn
    conn.close()
