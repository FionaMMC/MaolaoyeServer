from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.exceptions import APIError
from app.models import EmergencyExecution, HydraExecutionAttempt, HydraTarget, InstanceState, Order, OrderSignalMap
from app.schemas.emergency_execution import EmergencyStageRequest, EmergencyResumeRequest
from app.services import emergency_execution as emergency
from app.services.emergency_guard import assert_no_emergency
from app.services.orders_queue import OrdersQueueService
from tests.unit.test_hydra_relay import _setup, _target, _bytes, _price_frame
import hashlib


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(emergency, "_now", lambda: datetime(2026, 8, 4, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
    return _setup(tmp_path, live_enabled=True, state_domain="live", risk_mode="auto")


def request(setup, **changes):
    payload = dict(execution_domain="live", account_alias="hydra-live", instance_id="live_hydra",
        request_id="incident-001", operator="owner", reason="urgent risk reduction", confirm_emergency=True,
        trade_date="20260804", execution_calendar_sha256=setup[-1],
        execution_raw_sha256=hashlib.sha256(_bytes(_price_frame("20260803"))).hexdigest(),
        orders=[dict(symbol="510300.SH", direction="BUY", quantity=100, limit_price=4.019)])
    payload.update(changes)
    return EmergencyStageRequest(**payload)


def test_emergency_has_exact_orders_lineage_audit_and_replay(setup):
    service, sf, *_ = setup
    req = request(setup)
    first = emergency.stage_emergency(service, req, "authenticated-live-client")
    second = emergency.stage_emergency(service, req, "authenticated-live-client")
    assert second["idempotent_replay"] and second["batch_id"] == first["batch_id"]
    with sf() as session:
        assert len(list(session.scalars(select(Order)))) == 1
        order = session.scalar(select(Order))
        assert (order.quantity, order.limit_price) == (100, 4.019)
        assert session.scalar(select(OrderSignalMap)).order_id == order.order_id
        entry = session.get(EmergencyExecution, first["authorization_id"])
        assert entry.authenticated_client == "authenticated-live-client"
        attempt = session.get(HydraExecutionAttempt, first["attempt_id"])
        assert attempt.risk_snapshot["emergency_authorization"]["reason"] == req.reason
        with pytest.raises(APIError, match="紧急执行期间"):
            assert_no_emergency(session, "live", req.account_alias)
        assert_no_emergency(session, "paper", req.account_alias)
        assert_no_emergency(session, "live", "another-account")
    items = OrdersQueueService(sf).list_pending(req.trade_date, "live", (req.account_alias,))
    assert len(items) == 1 and items[0].batch_sha256 == first["batch_sha256"]


def test_changed_request_id_is_rejected_and_next_request_stays_held(setup):
    service = setup[0]
    emergency.stage_emergency(service, request(setup), "owner")
    with pytest.raises(APIError, match="内容不同"):
        emergency.stage_emergency(service, request(setup, reason="different"), "owner")
    with pytest.raises(APIError, match="已有紧急执行"):
        emergency.stage_emergency(service, request(setup, request_id="incident-002"), "owner")


def test_same_month_override_retires_only_unfetched_normal_batch(setup):
    service, sf, _, model, raw, actions, calendar = setup
    target = _target(model, raw, actions, calendar, execution_domain="live", account_alias="hydra-live", instance_id="live_hydra")
    old = service.stage_initial(target)
    result = emergency.stage_emergency(service, request(setup), "owner")
    with sf() as session:
        assert session.get(HydraTarget, old.target_id).status == "SUPERSEDED_EMERGENCY"
        assert all(order.status == "CANCELLED" for order in session.scalars(select(Order).where(Order.target_id == old.target_id)))
        assert session.get(HydraExecutionAttempt, result["attempt_id"]).risk_snapshot["emergency_authorization"]["retired_undelivered_orders"]
    with pytest.raises(APIError, match="紧急执行期间"):
        service.stage_initial(target)


def test_fetched_old_order_blocks_without_mutation(setup):
    service, sf, _, model, raw, actions, calendar = setup
    service.stage_initial(_target(model, raw, actions, calendar, execution_domain="live", account_alias="hydra-live", instance_id="live_hydra"))
    OrdersQueueService(sf).list_pending("20260804", "live", ("hydra-live",))
    with pytest.raises(APIError, match="领取"):
        emergency.stage_emergency(service, request(setup), "owner")
    with sf() as session:
        assert session.scalar(select(EmergencyExecution)) is None
        assert all(order.status == "PENDING" for order in session.scalars(select(Order)))


@pytest.mark.parametrize("changes,pattern", [
    ({"account_alias": "other"}, "跨 account_alias"),
    ({"trade_date": "20260803"}, "当日"),
    ({"orders": [dict(symbol="510300.SH", direction="BUY", quantity=100, limit_price=4.1)]}, "偏移"),
    ({"orders": [dict(symbol="510300.SH", direction="SELL", quantity=100, limit_price=4)]}, "归属持仓"),
    ({"orders": [dict(symbol="510300.SH", direction="BUY", quantity=99999900, limit_price=4)]}, "上限"),
    ({"orders": [dict(symbol="600000.SH", direction="BUY", quantity=100, limit_price=4)]}, "白名单"),
])
def test_hard_execution_checks_remain(setup, changes, pattern):
    with pytest.raises(APIError, match=pattern):
        emergency.stage_emergency(setup[0], request(setup, **changes), "owner")
    with setup[1]() as session:
        assert session.scalar(select(EmergencyExecution)) is None
        assert session.scalar(select(Order)) is None


def test_resume_requires_final_broker_reconciliation_and_never_revives_old_plan(setup):
    service, sf, *_ = setup
    req = request(setup)
    result = emergency.stage_emergency(service, req, "owner")
    resume = EmergencyResumeRequest(account_alias=req.account_alias, instance_id=req.instance_id,
        request_id=req.request_id, reason="new plan after review", confirm_new_plan_required=True)
    with pytest.raises(APIError):
        emergency.resume_after_emergency(service, resume, "owner")
    with sf() as session:
        session.scalar(select(Order)).status = "CANCELLED"
        session.commit()
    with pytest.raises(APIError, match="最终结算"):
        emergency.resume_after_emergency(service, resume, "owner")
    with sf() as session:
        attempt = session.get(HydraExecutionAttempt, result["attempt_id"])
        attempt.status = "RESIDUAL"
        session.commit()
    assert emergency.resume_after_emergency(service, resume, "owner")["new_plan_required"]
    assert emergency.stage_emergency(service, req, "owner")["authorization_status"] == "RELEASED"
    assert emergency.resume_after_emergency(service, resume, "owner")["idempotent_replay"]


@pytest.mark.parametrize("change", [{"reason": " "}, {"confirm_emergency": False}, {"execution_domain": "paper"},
    {"orders": [dict(symbol="510300.SH", direction="BUY", quantity=10, limit_price=4)]},
    {"orders": [dict(symbol="510300.SH", direction="BUY", quantity=100, limit_price=4.0001)]}])
def test_request_must_be_explicit_and_well_formed(setup, change):
    with pytest.raises(ValidationError):
        request(setup, **change)


def test_api_rejects_paper_backup_and_cross_account_tokens(client, settings_for_test):
    settings_for_test.live_api_key = "LIVE_KEY"
    settings_for_test.live_client_id = "live-client"
    settings_for_test.live_account_aliases_csv = "allowed"
    settings_for_test.live_data_backup_api_key = "BACKUP_KEY"
    payload = dict(execution_domain="live", account_alias="other", instance_id="live_hydra",
        request_id="incident-001", operator="owner", reason="urgent", confirm_emergency=True,
        trade_date="20260804", execution_calendar_sha256="1" * 64, execution_raw_sha256="2" * 64,
        orders=[dict(symbol="510300.SH", direction="BUY", quantity=100, limit_price=4)])
    for key in ("TEST_KEY", "LIVE_KEY", "BACKUP_KEY"):
        response = client.post("/hydra/emergency/stage", json=payload, headers={"Authorization": f"Bearer {key}"})
        assert response.status_code == 403


def test_standard_client_accepts_emergency_as_normal_signal_and_submits_once(setup, tmp_path, monkeypatch):
    import json
    from live_client import cli
    from live_client.tests.test_live_client import _cfg

    service, sf, *_ = setup
    req = request(setup)
    cfg = _cfg(tmp_path)
    staged = emergency.stage_emergency(service, req, "owner")

    class Server:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_orders(self, date):
            return [item.model_dump() for item in OrdersQueueService(sf).list_pending(date, "live", (req.account_alias,))]

        def reconcile(self, payload):
            return {"n_mismatched": 0, "n_server_only": 0, "n_qmt_only": 0, "cash_diff": 0}

    monkeypatch.setattr(cli, "LiveServerClient", Server)
    mock = tmp_path / "mock.json"
    mock.write_text(json.dumps({"account_id": cfg.account_id, "available_cash": 1000000, "positions": {}}))
    assert cli.query(cfg, req.trade_date)["batch_sha256"] == staged["batch_sha256"]
    cli.preflight(cfg, req.trade_date, mock)
    first = cli.submit(cfg, req.trade_date, mock)
    second = cli.submit(cfg, req.trade_date, mock)
    assert first["attempted_now"] == 1 and second["attempted_now"] == 0


def test_emergency_fill_updates_existing_ledger_once(setup):
    from app.services.settlement import SettlementService
    from app.schemas.trade_result import TradeResult
    service, sf, *_ = setup
    req = request(setup)
    emergency.stage_emergency(service, req, "owner")
    order = OrdersQueueService(sf).list_pending(req.trade_date, "live", (req.account_alias,))[0]
    result = TradeResult(order_id=order.order_id, filled_quantity=100, filled_price=4.019,
                         status="FILLED", filled_time="2026-08-04T10:00:00+08:00")
    settlement = SettlementService(sf)
    settlement.settle(req.trade_date, [result], execution_domain="live", allowed_account_aliases=(req.account_alias,))
    with sf() as session:
        state = session.get(InstanceState, req.instance_id)
        assert state.virtual_positions == {"510300.SH": 100}
        cash = state.virtual_cash
        assert cash < 1000000
    settlement.settle(req.trade_date, [result], execution_domain="live", allowed_account_aliases=(req.account_alias,))
    with sf() as session:
        state = session.get(InstanceState, req.instance_id)
        assert state.virtual_cash == cash and state.virtual_positions == {"510300.SH": 100}


def test_next_day_emergency_flows_through_existing_evening_advance(setup, monkeypatch):
    from tests.unit.test_hydra_relay import _install
    from app.schemas.hydra_relay import HydraAdvanceRequest
    from app.services.hydra_execution_advance import advance_execution
    service, sf, store, *_ = setup
    monkeypatch.setattr(emergency, "_now", lambda: datetime(2026, 8, 4, 18, tzinfo=ZoneInfo("Asia/Shanghai")))
    raw = _install(store, "hydra_execution_raw", _price_frame("20260804"), "none", "20260804")
    staged = emergency.stage_emergency(service, request(setup, trade_date="20260805", execution_raw_sha256=raw), "owner")
    req = HydraAdvanceRequest(account_alias="hydra-live", instance_id="live_hydra", reference_date="20260804",
        actual_cash=1000000, actual_positions={}, reconciliation_evidence_sha256="a"*64)
    result = advance_execution(service, req)
    assert result["status"] == "EXECUTION_ADVANCED"
    assert result["results"][0]["batch_sha256"] == staged["batch_sha256"]
    with sf() as session:
        assert len(list(session.scalars(select(Order)))) == 1


def test_emergency_cannot_race_pipeline_clear_and_recompute(setup):
    import fcntl
    service, sf, *_ = setup
    with sf() as session:
        database = session.get_bind().url.database
    with open(database + ".pipeline.lock", "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(APIError, match="普通管线正在运行"):
            emergency.stage_emergency(service, request(setup), "owner")
    with sf() as session:
        assert session.scalar(select(Order)) is None
