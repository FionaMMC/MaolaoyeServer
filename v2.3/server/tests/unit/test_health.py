"""tests/unit/test_health.py"""

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.ownership import OwnershipOverlap


def test_healthz_always_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_when_dependencies_ok(client, settings_for_test):
    # parquet_root 在 fixture 里已经创建好
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert "checks" in body
    assert body["checks"]["parquet_root"] == "ok"
    assert body["checks"]["ownership"] == "ok"


def test_readyz_503_when_parquet_missing(client, settings_for_test):
    # 删除 parquet_root 模拟未就绪
    import shutil
    shutil.rmtree(settings_for_test.parquet_root)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["parquet_root"] != "ok"


def test_ownership_overlap_does_not_kill_http_service(
    settings_for_test, monkeypatch,
):
    def fail_validation(_self):
        raise OwnershipOverlap("same-account overlap")

    monkeypatch.setattr(
        "app.main.ReconcileService.validate_no_overlap", fail_validation,
    )
    app = create_app(settings_override=settings_for_test)
    with TestClient(app) as client:
        response = client.get("/healthz")
        ready = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready_degraded"
    assert ready.json()["checks"]["ownership"] == "degraded: scheduler isolated"
