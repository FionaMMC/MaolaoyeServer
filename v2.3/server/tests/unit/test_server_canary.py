from fastapi.testclient import TestClient

from app.dependencies import _engine_for_url
from app.main import create_app
from app.settings import Settings, get_settings


def _client(tmp_path, enabled: bool) -> TestClient:
    get_settings.cache_clear(); _engine_for_url.cache_clear()
    settings = Settings(
        db_url=f"sqlite:///{tmp_path}/canary.db", parquet_root=tmp_path / "data",
        plugins_dir=tmp_path / "plugins", strategies_file=tmp_path / "strategies.yaml",
        live_api_key="LIVE", live_client_id="hydra-live",
        live_account_aliases_csv="hydra-live", live_canary_staging_enabled=enabled,
    )
    settings.parquet_root.mkdir(exist_ok=True); settings.plugins_dir.mkdir(exist_ok=True)
    return TestClient(create_app(settings_override=settings))


def _payload():
    return {"execution_domain":"live", "account_alias":"hydra-live", "trade_date":"20260831",
            "plan_sha256":"a" * 64, "symbol":"510300.SH", "quantity":100,
            "reference_price":4.0, "limit_price":4.0}


def test_server_canary_is_gated_idempotent_and_deliverable(tmp_path):
    closed = _client(tmp_path, False)
    headers = {"Authorization": "Bearer LIVE"}
    assert closed.post("/hydra/canary/stage", json=_payload(), headers=headers).status_code == 423
    client = _client(tmp_path, True)
    first = client.post("/hydra/canary/stage", json=_payload(), headers=headers)
    assert first.status_code == 200
    second = client.post("/hydra/canary/stage", json=_payload(), headers=headers)
    assert second.status_code == 200
    # Delivery remains independently gated; staging does not grant order pickup.
    assert client.get("/orders?date=20260831", headers=headers).status_code == 423
