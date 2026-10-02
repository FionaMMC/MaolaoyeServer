"""Manual instructions and dividend registration from the dashboard (operator key only)."""
from fastapi.testclient import TestClient

from app.db import make_session_factory
from app.dependencies import _engine_for_url, get_settings
from app.main import create_app
from app.models import DividendEntitlement, InstanceState, Order
from app.oms.models import OmsManualInstruction, OmsOrder, OmsOverride
from app.settings import Settings

LIVE = {"Authorization": "Bearer LIVE_KEY"}
OPERATOR = {"Authorization": "Bearer OP_KEY"}
PAPER = {"Authorization": "Bearer PAPER_KEY"}


def _client(tmp_path):
    get_settings.cache_clear()
    _engine_for_url.cache_clear()
    settings = Settings(paper_api_key="PAPER_KEY", live_api_key="LIVE_KEY", oms_operator_api_key="OP_KEY",
                        live_client_id="hydra-live-client", live_account_aliases_csv="hydra-live",
                        oms_live_enabled=True, db_url=f"sqlite:///{tmp_path}/manual.db",
                        parquet_root=tmp_path / "data", plugins_dir=tmp_path / "plugins",
                        strategies_file=tmp_path / "strategies.yaml", log_level="WARNING")
    client = TestClient(create_app(settings_override=settings))
    sf = make_session_factory(_engine_for_url(settings.db_url))
    with sf() as s:
        s.add(InstanceState(instance_id="live_hydra_v481_rb", execution_domain="live", account_alias="hydra-live",
                            ledger_mode="attributed", virtual_cash=100000.0, virtual_positions={"511260.SH": 1200},
                            owned_symbols=["511260.SH", "510300.SH"], last_update="x"))
        s.commit()
    return client, sf


ORDER = {"account_alias": "hydra-live", "symbol": "510300.SH", "side": "BUY", "quantity": 500,
         "limit_price": 4.621, "reason": "manual top-up after review", "operator": "fiona", "confirm": True}


def test_operator_key_reaches_operator_routes_only(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/oms/live/manual/orders", json=ORDER, headers=OPERATOR).json()["code"] == 0
    assert client.get("/oms/live/overview", params={"account_alias": "hydra-live"}, headers=OPERATOR).json()["code"] == 0
    # The operator cannot impersonate the agent, and the agent/paper keys cannot issue manual orders.
    assert client.get("/oms/live/manual/pending", params={"account_alias": "hydra-live"},
                      headers=OPERATOR).status_code == 403
    assert client.post("/oms/live/snapshot", json={}, headers=OPERATOR).status_code == 403
    for headers in (LIVE, PAPER):
        assert client.post("/oms/live/manual/orders", json=ORDER, headers=headers).status_code == 403


def test_manual_order_is_an_e_order_in_the_ledger_and_reaches_the_agent(tmp_path):
    client, sf = _client(tmp_path)
    data = client.post("/oms/live/manual/orders", json=ORDER, headers=OPERATOR).json()["data"]
    coid = data["client_order_id"]
    assert len(coid) == 10 and coid.startswith("E")
    with sf() as s:
        order = s.get(OmsOrder, coid)
        assert order.state == "PLANNED" and order.cycle_id == "MANUAL" and order.quantity == 500
        assert s.get(Order, coid).status == "OMS_ROUTED"                 # projects into the Hydra ledger
        assert s.query(OmsOverride).filter_by(action="MANUAL_ORDER").one().operator == "fiona"
    pending = client.get("/oms/live/manual/pending", params={"account_alias": "hydra-live",
                                                              "trade_date": data["trade_date"]}, headers=LIVE)
    item = pending.json()["data"]["instructions"][0]
    assert item["kind"] == "ORDER" and item["client_order_id"] == coid and item["limit_price"] == 4.621
    ack = client.post("/oms/live/manual/ack", json={"account_alias": "hydra-live", "results": [
        {"instruction_id": item["instruction_id"], "status": "SUBMITTED", "detail": "qmt 1001"}]}, headers=LIVE)
    assert ack.json()["data"]["updated"] == 1
    again = client.get("/oms/live/manual/pending", params={"account_alias": "hydra-live",
                                                            "trade_date": data["trade_date"]}, headers=LIVE)
    assert again.json()["data"]["instructions"] == []


def test_manual_cancel_by_client_or_broker_id(tmp_path):
    client, sf = _client(tmp_path)
    body = {"account_alias": "hydra-live", "broker_order_id": "1082138153", "reason": "stuck order", "operator": "fiona",
            "confirm": True}
    data = client.post("/oms/live/manual/cancels", json=body, headers=OPERATOR).json()["data"]
    with sf() as s:
        row = s.get(OmsManualInstruction, data["instruction_id"])
        assert row.kind == "CANCEL" and row.broker_order_id == "1082138153" and row.status == "PENDING"
    neither = dict(body)
    neither.pop("broker_order_id")
    assert client.post("/oms/live/manual/cancels", json=neither, headers=OPERATOR).json()["code"] != 0


def test_manual_order_validation(tmp_path):
    client, _ = _client(tmp_path)
    for change in ({"symbol": "600519.SH"}, {"quantity": 150}, {"limit_price": 4.6215}, {"reason": ""},
                   {"confirm": False}, {"account_alias": "someone-else"}):
        response = client.post("/oms/live/manual/orders", json=dict(ORDER, **change), headers=OPERATOR)
        assert response.status_code != 200 or response.json()["code"] != 0, change


DIVIDEND = {"account_alias": "hydra-live", "symbol": "511260.SH", "record_date": "20261015", "ex_date": "20261016",
            "pay_date": "20261020", "entitled_quantity": 1200, "cash_per_share": 0.35,
            "evidence": "SSE announcement 2026-10-10 511260 dividend", "operator": "fiona", "reason": "fund dividend"}


def test_dividend_preview_writes_nothing_then_register_writes_entitlement(tmp_path):
    client, sf = _client(tmp_path)
    preview = client.post("/oms/live/dividends/preview", json=DIVIDEND, headers=OPERATOR).json()["data"]
    assert preview["amount"] == "420.00" and preview["applied"] is False
    with sf() as s:
        assert s.query(DividendEntitlement).count() == 0
    done = client.post("/oms/live/dividends", json=DIVIDEND, headers=OPERATOR).json()["data"]
    assert done["applied"] is True
    with sf() as s:
        row = s.query(DividendEntitlement).one()
        assert row.instance_id == "live_hydra_v481_rb" and row.amount == "420.00"
        assert s.query(OmsOverride).filter_by(action="DIVIDEND_REGISTERED").count() == 1


def test_overview_has_everything_the_dashboard_shows(tmp_path):
    client, _ = _client(tmp_path)
    client.post("/oms/live/manual/orders", json=ORDER, headers=OPERATOR)
    data = client.get("/oms/live/overview", params={"account_alias": "hydra-live"}, headers=OPERATOR).json()["data"]
    assert set(data) >= {"status", "ledger", "instructions", "overrides", "dividends", "latest_snapshot", "symbols"}
    assert data["ledger"]["positions"] == {"511260.SH": 1200} and len(data["instructions"]) == 1


def test_dashboard_page_is_served_without_data(tmp_path):
    client, _ = _client(tmp_path)
    page = client.get("/dashboard/oms")
    assert page.status_code == 200 and "人工下单" in page.text and "运维密钥" in page.text
    assert "511260" not in page.text                                    # no data before the key is entered
