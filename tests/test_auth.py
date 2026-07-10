import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from diary.auth import AccessJWTMiddleware
from diary.config import Settings


@pytest.fixture
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJWKSClient:
    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token):
        class _Key:
            key = self.public_key
        return _Key()


def _settings(**overrides):
    defaults = dict(
        db_path=":memory:", llm_model="test", batch_concurrency=1, llm_concurrency=4,
        cf_team_domain="myteam", cf_access_aud="test-aud",
        operator_email="owner@example.com", require_access_auth=True,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_app(settings, public_key):
    app = FastAPI()
    app.add_middleware(AccessJWTMiddleware, settings=settings, jwks_client=_FakeJWKSClient(public_key))

    @app.get("/protected")
    async def protected():
        return {"ok": True}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app


def _make_token(private_key, **claim_overrides):
    now = int(time.time())
    claims = {
        "aud": "test-aud", "email": "owner@example.com",
        "iat": now, "exp": now + 3600, "nbf": now - 10, "iss": "https://myteam.cloudflareaccess.com",
    }
    claims.update(claim_overrides)
    return pyjwt.encode(claims, private_key, algorithm="RS256")


def test_missing_token_rejected(private_key):
    app = _make_app(_settings(), private_key.public_key())
    client = TestClient(app)
    response = client.get("/protected")
    assert response.status_code == 403


def test_valid_token_accepted(private_key):
    app = _make_app(_settings(), private_key.public_key())
    client = TestClient(app)
    token = _make_token(private_key)
    response = client.get("/protected", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 200


def test_expired_token_rejected(private_key):
    app = _make_app(_settings(), private_key.public_key())
    client = TestClient(app)
    token = _make_token(private_key, exp=int(time.time()) - 3600, iat=int(time.time()) - 7200)
    response = client.get("/protected", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_wrong_audience_rejected(private_key):
    app = _make_app(_settings(), private_key.public_key())
    client = TestClient(app)
    token = _make_token(private_key, aud="someone-elses-app")
    response = client.get("/protected", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_wrong_email_rejected(private_key):
    app = _make_app(_settings(), private_key.public_key())
    client = TestClient(app)
    token = _make_token(private_key, email="intruder@example.com")
    response = client.get("/protected", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_wrong_issuer_rejected(private_key):
    app = _make_app(_settings(), private_key.public_key())
    client = TestClient(app)
    token = _make_token(private_key, iss="https://attacker-team.cloudflareaccess.com")
    response = client.get("/protected", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403


def test_healthz_exempt_from_auth(private_key):
    app = _make_app(_settings(), private_key.public_key())
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200


def test_auth_can_be_disabled_for_local_dev(private_key):
    app = _make_app(_settings(require_access_auth=False), private_key.public_key())
    client = TestClient(app)
    response = client.get("/protected")
    assert response.status_code == 200
