"""/oms/live/* is reachable only with the live client token for its own account."""
from fastapi.testclient import TestClient

from app.db import make_session_factory
from app.dependencies import _engine_for_url, get_settings
from app.main import create_app
from app.models import InstanceState
from app.settings import Settings

LIVE = {"Authorization": "Bearer LIVE_KEY"}
PAPER = {"Authorization": "Bearer PAPER_KEY"}
TRIGGER = {"Authorization": "Bearer TRIGGER_KEY"}
CAL = ["20260930", "20261008", "20261009", "20261012", "20261013", "20261014"]


def _client(tmp_path, *, oms_enabled):
    get_settings.cache_clear()
    _engine_for_url.cache_clear()
    settings = Settings(paper_api_key="PAPER_KEY", live_api_key="LIVE_KEY", live_trigger_api_key="TRIGGER_KEY",
                        live_client_id="hydra-live-client", live_account_aliases_csv="hydra-live",
                        oms_live_enabled=oms_enabled, db_url=f"sqlite:///{tmp_path}/api.db",
                        parquet_root=tmp_path / "data", plugins_dir=tmp_path / "plugins",
                        strategies_file=tmp_path / "strategies.yaml", log_level="WARNING")
    client = TestClient(create_app(settings_override=settings))
    sf = make_session_factory(_engine_for_url(settings.db_url))
    with sf() as s:
        s.add(InstanceState(instance_id="live_hydra_v481_rb", execution_domain="live", account_alias="hydra-live",
                            ledger_mode="attributed", virtual_cash=100000.0, virtual_positions={"510300.SH": 3000},
                            owned_symbols=["510300.SH", "513100.SH"], last_update="x"))
        s.commit()
    return client, sf


def _publish_and_approve(client):
    from app.dependencies import get_oms_cycle_service
    service = get_oms_cycle_service(*_deps(client))
    service.publish_target(instance_id="live_hydra_v481_rb", account_alias="hydra-live", signal_date="20260930",
                           weights={"510300.SH": 0.05, "513100.SH": 0.95},
                           signal_closes={"510300.SH": 4.6, "513100.SH": 2.2}, calendar=CAL,
                           source_sha256="s" * 64, now="2026-10-07T20:00:00+08:00")
    return service


def _deps(client):
    settings = client.app.dependency_overrides[get_settings]()
    sf = make_session_factory(_engine_for_url(settings.db_url))
    return sf, settings


SNAP = {"account_alias": "hydra-live", "kind": "PRE", "trade_date": "20261008",
        "taken_at": "2026-10-08T14:45:00+08:00", "available_cash": 100000.0, "total_asset": 113800.0,
        "positions": {"510300.SH": 3000}, "sellable": {"510300.SH": 3000}, "orders": [], "trades": None,
        "quotes": {}}


def test_live_token_reaches_all_oms_routes_and_others_are_refused(tmp_path):
    client, _ = _client(tmp_path, oms_enabled=False)
    assert client.post("/oms/live/snapshot", json=SNAP, headers=LIVE).json()["code"] == 0
    assert client.post("/oms/live/events", json={"account_alias": "hydra-live", "events": []},
                       headers=LIVE).json()["code"] == 0
    assert client.get("/oms/live/status", params={"account_alias": "hydra-live"}, headers=LIVE).json()["code"] == 0
    for headers in (PAPER, TRIGGER):
        assert client.post("/oms/live/snapshot", json=SNAP, headers=headers).status_code == 403


def test_foreign_account_alias_is_refused(tmp_path):
    client, _ = _client(tmp_path, oms_enabled=True)
    body = dict(SNAP, account_alias="someone-else")
    assert client.post("/oms/live/snapshot", json=body, headers=LIVE).status_code == 403
    assert client.get("/oms/live/status", params={"account_alias": "someone-else"}, headers=LIVE).status_code == 403


def test_plan_is_not_executable_while_flag_off_or_cycle_unapproved(tmp_path):
    client, _ = _client(tmp_path, oms_enabled=False)
    service = _publish_and_approve(client)
    params = {"account_alias": "hydra-live", "trade_date": "20261008", "phase": "SELL"}
    plan = client.get("/oms/live/plan", params=params, headers=LIVE).json()["data"]
    assert plan["executable"] is False and plan["orders"][0]["side"] == "SELL"
    service.approve("C00001", "tester", "n")
    assert client.get("/oms/live/plan", params=params, headers=LIVE).json()["data"]["executable"] is False


def test_plan_executable_with_flag_on_and_cycle_active(tmp_path):
    client, _ = _client(tmp_path, oms_enabled=True)
    service = _publish_and_approve(client)
    params = {"account_alias": "hydra-live", "trade_date": "20261008", "phase": "SELL"}
    assert client.get("/oms/live/plan", params=params, headers=LIVE).json()["data"]["executable"] is False
    service.approve("C00001", "tester", "n")
    assert client.get("/oms/live/plan", params=params, headers=LIVE).json()["data"]["executable"] is True
    missing = client.get("/oms/live/plan", params=dict(params, trade_date="20261020"), headers=LIVE)
    assert missing.status_code == 404


def test_same_snapshot_is_stored_once(tmp_path):
    client, sf = _client(tmp_path, oms_enabled=True)
    first = client.post("/oms/live/snapshot", json=SNAP, headers=LIVE).json()["data"]
    second = client.post("/oms/live/snapshot", json=SNAP, headers=LIVE).json()["data"]
    assert first["snapshot_id"] == second["snapshot_id"]
    from app.oms.models import OmsBrokerSnapshot
    with sf() as s:
        assert s.query(OmsBrokerSnapshot).count() == 1
