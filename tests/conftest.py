import pytest
from fastapi.testclient import TestClient

from diary.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DIARY_DB", db_path)
    monkeypatch.setenv("DIARY_REQUIRE_ACCESS_AUTH", "false")
    app = create_app()
    with TestClient(app) as c:
        yield c
