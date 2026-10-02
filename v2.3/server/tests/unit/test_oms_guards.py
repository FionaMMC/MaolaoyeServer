"""Once the execution core owns live, no legacy path may write live order state."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.dependencies import _engine_for_url, get_settings
from app.main import create_app
from app.scheduler.pipeline import StrategyPipeline
from app.settings import Settings

LIVE = {"Authorization": "Bearer LIVE_KEY"}
PAPER = {"Authorization": "Bearer PAPER_KEY"}


def _client(tmp_path, *, oms_enabled):
    get_settings.cache_clear()
    _engine_for_url.cache_clear()
    settings = Settings(paper_api_key="PAPER_KEY", live_api_key="LIVE_KEY", live_trigger_api_key="TRIGGER_KEY",
                        live_client_id="hydra-live-client", live_account_aliases_csv="hydra-live",
                        live_order_delivery_enabled=True, oms_live_enabled=oms_enabled,
                        db_url=f"sqlite:///{tmp_path}/guards.db", parquet_root=tmp_path / "data",
                        plugins_dir=tmp_path / "plugins", strategies_file=tmp_path / "strategies.yaml",
                        log_level="WARNING")
    return TestClient(create_app(settings_override=settings))


def test_pipeline_refuses_live_domain_before_touching_anything():
    # No session factory at all: the refusal must happen before the mutex or any read.
    out = StrategyPipeline.run(SimpleNamespace(session_factory=None), 20261009, execution_domain="live")
    assert out["skipped"] == "live_domain_owned_by_oms" and out["orders"] == 0


LEGACY_LIVE_WRITES = [
    ("get", "/orders?date=20261009"),
    ("post", "/trade-result"),
    ("post", "/hydra/targets/stage"),
    ("post", "/hydra/rebalances/retry"),
    ("post", "/hydra/execution/advance"),
    ("post", "/hydra/attempts/close"),
    ("post", "/hydra/emergency/stage"),
    ("post", "/hydra/emergency/resume"),
    ("post", "/hydra/canary/stage"),
]


@pytest.mark.parametrize("method,path", LEGACY_LIVE_WRITES)
def test_legacy_live_writes_refuse_when_oms_enabled(tmp_path, method, path):
    client = _client(tmp_path, oms_enabled=True)
    kwargs = {"headers": LIVE} if method == "get" else {"headers": LIVE, "json": {}}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 409, (path, response.text)


def test_legacy_orders_unchanged_when_oms_disabled_and_for_paper(tmp_path):
    assert _client(tmp_path, oms_enabled=False).get("/orders?date=20261009", headers=LIVE).status_code == 200
    assert _client(tmp_path, oms_enabled=True).get("/orders?date=20261009", headers=PAPER).status_code == 200
