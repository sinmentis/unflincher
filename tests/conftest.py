import pytest
from fastapi.testclient import TestClient

import diary.llm as llm_module
from diary.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DIARY_DB", db_path)
    monkeypatch.setenv("DIARY_REQUIRE_ACCESS_AUTH", "false")

    async def _noop():
        pass

    monkeypatch.setattr(llm_module, "warm_up_client", _noop)
    monkeypatch.setattr(llm_module, "shutdown_client", _noop)

    app = create_app()
    with TestClient(app) as c:
        c.get("/healthz")  # any GET response sets the csrf_token cookie
        token = c.cookies.get("csrf_token")
        c.headers.update({"X-CSRF-Token": token})
        yield c
