"""tests/unit/test_auth.py — Bearer token auth dependency"""
from fastapi import Depends
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


def test_auth_binds_distinct_tokens_to_execution_domains():
    settings = Settings(
        paper_api_key="PAPER_KEY",
        live_api_key="LIVE_KEY",
        live_client_id="hydra-live-client",
        live_account_aliases_csv="hydra-live",
        log_level="WARNING",
    )
    app = create_app(settings_override=settings)

    @app.get("/cash-flows")
    async def domain(auth=Depends(verify_api_key)):
        return {"execution_domain": auth.execution_domain}

    client = TestClient(app)
    paper = client.get(
        "/cash-flows", headers={"Authorization": "Bearer PAPER_KEY"},
    )
    live = client.get(
        "/cash-flows", headers={"Authorization": "Bearer LIVE_KEY"},
    )
    assert paper.json() == {"execution_domain": "paper"}
    assert live.json() == {"execution_domain": "live"}


def test_live_token_denies_routes_not_explicitly_domain_audited():
    settings = Settings(
        paper_api_key="PAPER_KEY",
        live_api_key="LIVE_KEY",
        live_client_id="hydra-live-client",
        live_account_aliases_csv="hydra-live",
        log_level="WARNING",
    )
    client = TestClient(create_app(settings_override=settings))
    response = client.get(
        "/admin/ops/pipeline-runs",
        headers={"Authorization": "Bearer LIVE_KEY"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == 1001


def test_settings_rejects_shared_paper_live_token():
    import pytest

    with pytest.raises(ValueError, match="必须不同"):
        Settings(paper_api_key="SAME", live_api_key="SAME")
