import pytest
from fastapi.testclient import TestClient

from diary.app import app


@pytest.fixture
def client():
    return TestClient(app)
