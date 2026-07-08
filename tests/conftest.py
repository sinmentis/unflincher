import pytest
from fastapi.testclient import TestClient

from diary.app import app
from diary.db import get_connection, init_schema


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DIARY_DB", db_path)
    conn = get_connection(db_path)
    init_schema(conn)
    app.state.db = conn
    with TestClient(app) as c:
        yield c
    conn.close()
