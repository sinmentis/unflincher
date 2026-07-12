from fastapi import FastAPI
from fastapi.testclient import TestClient

from unflincher.csrf import CSRFMiddleware


def _make_app():
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.get("/form")
    async def form():
        return {"ok": True}

    @app.post("/mutate")
    async def mutate():
        return {"ok": True}

    return app


def test_get_request_sets_csrf_cookie():
    client = TestClient(_make_app())
    response = client.get("/form")
    assert "csrf_token" in response.cookies


def test_post_without_csrf_header_rejected():
    client = TestClient(_make_app())
    client.get("/form")  # obtain the cookie
    response = client.post("/mutate")
    assert response.status_code == 403


def test_post_with_matching_csrf_header_accepted():
    client = TestClient(_make_app())
    client.get("/form")
    token = client.cookies.get("csrf_token")
    response = client.post("/mutate", headers={"X-CSRF-Token": token})
    assert response.status_code == 200


def test_post_with_mismatched_csrf_header_rejected():
    client = TestClient(_make_app())
    client.get("/form")
    response = client.post("/mutate", headers={"X-CSRF-Token": "wrong-value"})
    assert response.status_code == 403


def test_post_with_cross_site_origin_rejected_even_with_valid_token():
    client = TestClient(_make_app())
    client.get("/form")
    token = client.cookies.get("csrf_token")
    response = client.post(
        "/mutate",
        headers={"X-CSRF-Token": token, "Origin": "https://evil.example.com", "Host": "testserver"},
    )
    assert response.status_code == 403
