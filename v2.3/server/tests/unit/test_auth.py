"""tests/unit/test_auth.py — Bearer token auth dependency"""
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import verify_api_key
from app.main import create_app
from app.settings import Settings


def _build_test_app(api_key: str) -> TestClient:
    settings = Settings(api_key=api_key, log_level="WARNING")
    app = create_app(settings_override=settings)

    @app.get("/_protected")
    async def protected(_: None = Depends(verify_api_key)):
        return {"ok": True}

    return TestClient(app)


def test_auth_missing_header_returns_401():
    client = _build_test_app("KEY_ABC")
    resp = client.get("/_protected")
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == 1001


def test_auth_wrong_key_returns_401():
    client = _build_test_app("KEY_ABC")
    resp = client.get("/_protected", headers={"Authorization": "Bearer WRONG"})
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == 1001


def test_auth_correct_key_returns_200():
    client = _build_test_app("KEY_ABC")
    resp = client.get("/_protected", headers={"Authorization": "Bearer KEY_ABC"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_auth_malformed_header_returns_401():
    client = _build_test_app("KEY_ABC")
    resp = client.get("/_protected", headers={"Authorization": "KEY_ABC"})
    assert resp.status_code == 401
