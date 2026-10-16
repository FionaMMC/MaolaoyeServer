from datetime import datetime, timedelta, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from app.db import init_db, make_engine, make_session_factory
from app.exceptions import APIError
from app.models import (InstanceState, Order, RawSignal, OrderSignalMap, Trade,
                        CashFlowJournal, AccountingReceipt, ExecutionQualityObservation)
from app.oms.models import OmsBrokerSnapshot, OmsCycle, OmsOrder
from app.schemas.accounting_operations import (FeeAdjustmentRequest, CapitalTransferRequest,
    CorporateActionIssueRequest, CorporateActionResolutionRequest)
from app.schemas.trade_result import TradeResult
from app.services.accounting_operations import AccountingOperationsService
from app.services.settlement import SettlementService
from app.services.strategy_sellable import strategy_sellable

NOW = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
STAMP = NOW.isoformat()
DAY = "20261015"
STOCK = {"asset_type": "stock", "price_tick": .01, "commission_rate": .0003,
         "min_commission": 5., "stamp_duty_sell": .0005, "t_plus_one": True}


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/accounting.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        for instance in ("hydra", "live_v713"):
            s.add(InstanceState(instance_id=instance, execution_domain="live", account_alias="shared",
                ledger_mode="attributed", virtual_cash=20000., virtual_positions={"600000.SH": 1000},
                owned_symbols=["600000.SH", "510300.SH"], last_update=STAMP,
                strategy_state={"oms_instruments": {"600000.SH": STOCK}}))
        s.commit()
    yield sf, AccountingOperationsService(sf, clock=lambda: NOW)
    engine.dispose()


def command(**changes):
    return dict(execution_domain="live", account_alias="shared", event_id="e1",
        evidence_sha256="a" * 64, operator="test", reason="reviewed evidence", **changes)


def order(sf, *, oid="order1", owner="live_v713", side="SELL", status="OMS_ROUTED", symbol="600000.SH", quantity=1000):
    with sf() as s:
        s.add(Order(order_id=oid, execution_domain="live", qmt_account_alias="shared", target_id="target",
            account_group="shared", symbol=symbol, direction=side, quantity=quantity,
            limit_price=10., valid_date=DAY, status=status, created_at=STAMP))
        s.add(RawSignal(signal_id=oid, execution_domain="live", instance_id=owner,
            symbol=symbol, direction=side, quantity=quantity, reference_price=10., price_offset=0,
            limit_price=10., valid_date=DAY, signal_time=STAMP, precheck_status="PASS"))
        s.add(OrderSignalMap(order_id=oid, signal_id=oid, signal_quantity=quantity))
        s.commit()


def settle(sf, qty, *, oid="order1", side="SELL", status="FILLED"):
    return SettlementService(sf).settle(DAY, [TradeResult(order_id=oid, filled_quantity=qty,
        filled_price=10., filled_time=STAMP, status=status, symbol="600000.SH", direction=side)],
        "live", ("shared",))


def test_oms_stock_fee_not_etf_and_partial_commission_is_cumulative(env):
    sf, _ = env
    order(sf)
    assert settle(sf, 400, status="PARTIAL").matched_count == 1
    assert settle(sf, 1000).matched_count == 1
    settle(sf, 1000)
    with sf() as s:
        assert s.get(InstanceState, "live_v713").virtual_cash == 29990
        assert s.get(InstanceState, "hydra").virtual_cash == 20000
        assert s.get(ExecutionQualityObservation, "order1").estimated_fees == 10


def test_unknown_live_instrument_refuses_projection_until_explicit_metadata(env):
    sf, _ = env
    order(sf, symbol="600001.SH")
    result = settle(sf, 1000)
    assert result.rejected_observations == {"order1": "INSTRUMENT_FEE_SPEC_MISSING"}
    with sf() as s:
        assert s.query(Trade).count() == 0
        assert s.get(InstanceState, "live_v713").virtual_cash == 20000


def test_frozen_cycle_instrument_survives_state_spec_change(env):
    sf, _ = env
    order(sf)
    with sf() as s:
        s.add(OmsCycle(cycle_id="cycle", cycle_no=1, target_version_id="target", account_alias="shared",
            instance_id="live_v713", policy={"instruments": {"600000.SH": STOCK}}, nav_at_signal=30000,
            frozen_target={}, sell_anchor={}, lot_gap=0., schedule=[], status="ACTIVE", created_at=STAMP))
        s.add(OmsOrder(client_order_id="order1", session_id="session", cycle_id="cycle", account_alias="shared",
            trade_date=DAY, symbol="600000.SH", side="SELL", quantity=1000, limit_price=10.,
            reference_price=10., state="PLANNED", filled_qty=0, avg_price=0., projected_qty=0,
            created_at=STAMP, updated_at=STAMP))
        s.get(InstanceState, "live_v713").strategy_state = {"oms_instruments": {"600000.SH": STOCK | {"stamp_duty_sell": 0}}}
        s.commit()
    settle(sf, 1000)
    with sf() as s:
        assert s.get(InstanceState, "live_v713").virtual_cash == 29990


def test_actual_fee_correction_is_cumulative_audited_and_replayable(env):
    sf, service = env
    order(sf)
    settle(sf, 1000)
    req = FeeAdjustmentRequest(**command(order_id="order1", filled_quantity=1000, actual_cumulative_fees="12.50"))
    result = service.adjust_fees(req)
    assert result["cash_delta"] == "-2.50"
    assert service.adjust_fees(req)["already_applied"]
    changed = req.model_copy(update={"event_id": "revised", "actual_cumulative_fees": Decimal("11.00")})
    assert service.adjust_fees(changed)["cash_delta"] == "1.50"
    with sf() as s:
        assert s.get(InstanceState, "live_v713").virtual_cash == 29989
        assert s.query(AccountingReceipt).count() == 2
        assert sum(r.amount for r in s.scalars(select(CashFlowJournal))) == -1
    with pytest.raises(APIError, match="different content"):
        service.adjust_fees(req.model_copy(update={"actual_cumulative_fees": Decimal("13.00")}))


def test_fee_correction_requires_current_final_attributed_fill(env):
    sf, service = env
    order(sf)
    settle(sf, 400, status="PARTIAL")
    req = FeeAdjustmentRequest(**command(order_id="order1", filled_quantity=400, actual_cumulative_fees="5"))
    with pytest.raises(APIError, match="finalize"):
        service.adjust_fees(req)
    settle(sf, 1000)
    with pytest.raises(APIError, match="quantity changed"):
        service.adjust_fees(req)


def test_atomic_transfer_preserves_account_total_and_original_receipt(env):
    sf, service = env
    req = CapitalTransferRequest(**command(from_instance_id="hydra", to_instance_id="live_v713", amount="3000"))
    first = service.transfer_capital(req)
    with sf() as s:
        s.get(InstanceState, "live_v713").virtual_cash += 123
        s.commit()
    replay = service.transfer_capital(req)
    assert replay["already_applied"] and replay["to_cash_after"] == first["to_cash_after"] == 23000
    with sf() as s:
        assert s.get(InstanceState, "hydra").virtual_cash == 17000
        assert s.get(InstanceState, "live_v713").virtual_cash == 23123
        rows = s.scalars(select(CashFlowJournal)).all()
        assert len(rows) == 2 and sum(row.amount for row in rows) == 0


def test_transfer_cannot_spend_pending_unknown_or_custom_fee_reserve(env):
    sf, service = env
    order(sf, owner="hydra", side="BUY")
    with sf() as s:
        inst = s.get(InstanceState, "hydra")
        inst.strategy_state = {"oms_instruments": {"600000.SH": STOCK | {"buy_reserve_rate": .02}}}
        s.commit()
    req = CapitalTransferRequest(**command(from_instance_id="hydra", to_instance_id="live_v713", amount="9900"))
    with pytest.raises(APIError, match="reserved cash"):
        service.transfer_capital(req)
    with sf() as s:
        assert s.query(CashFlowJournal).count() == 0


def test_parallel_transfers_cannot_double_spend_source(env):
    sf, service = env
    def run(event):
        try:
            return service.transfer_capital(CapitalTransferRequest(**(command(
                from_instance_id="hydra", to_instance_id="live_v713", amount="15000") | {"event_id": event})))
        except APIError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("one", "two")))
    assert sum(result is not None for result in results) == 1
    with sf() as s:
        assert s.get(InstanceState, "hydra").virtual_cash == 5000
        assert s.get(InstanceState, "live_v713").virtual_cash == 35000


def test_strategy_t1_sellability_cannot_borrow_hydra_old_shares(env):
    sf, _ = env
    order(sf, side="BUY")
    settle(sf, 400, side="BUY", status="PARTIAL")
    settle(sf, 1000, side="BUY")
    settle(sf, 1000, side="BUY")
    with sf() as s:
        assert strategy_sellable(s, "live_v713", DAY)["600000.SH"] == 1000
        assert strategy_sellable(s, "hydra", DAY)["600000.SH"] == 1000
        assert strategy_sellable(s, "live_v713", "20261016")["600000.SH"] == 2000


def test_explicit_t0_rule_allows_same_day_owned_shares(env):
    sf, _ = env
    order(sf, side="BUY")
    settle(sf, 1000, side="BUY")
    with sf() as s:
        s.get(InstanceState, "live_v713").strategy_state = {"oms_instruments": {"600000.SH": STOCK | {"t_plus_one": False}}}
        s.commit()
    with sf() as s:
        assert strategy_sellable(s, "live_v713", DAY)["600000.SH"] == 2000


def snapshot(sf, *, identifier="snapshot", quantity=2100, cash=40000, at=STAMP):
    with sf() as s:
        s.add(OmsBrokerSnapshot(snapshot_id=identifier, account_alias="shared", kind="ADHOC", trade_date=DAY,
            taken_at=at, received_at=STAMP, payload={"positions": {"600000.SH": quantity}, "available_cash": cash}))
        s.commit()


def test_corporate_action_stops_only_affected_owner_and_resolution_is_exact(env):
    sf, service = env
    issue = CorporateActionIssueRequest(**command(instance_id="live_v713", symbol="600000.SH", action_type="STOCK_DISTRIBUTION"))
    service.register_issue(issue)
    assert service.register_issue(issue)["already_applied"]
    with sf() as s:
        assert s.get(InstanceState, "live_v713").strategy_state["accounting_hold"]
        assert not s.get(InstanceState, "hydra").strategy_state.get("accounting_hold")
    snapshot(sf)
    req = CorporateActionResolutionRequest(**(command(instance_id="live_v713", issue_id="e1",
        expected_quantity=1000, quantity_delta=100, expected_cash="20000", broker_snapshot_id="snapshot") | {"event_id": "resolve"}))
    assert service.resolve_issue(req)["quantity_after"] == 1100
    assert service.resolve_issue(req)["already_applied"]
    with sf() as s:
        assert not s.get(InstanceState, "live_v713").strategy_state["accounting_hold"]
        assert s.get(InstanceState, "hydra").virtual_positions == {"600000.SH": 1000}
        assert s.query(CashFlowJournal).count() == 0


def test_corporate_resolution_rejects_old_or_unfunded_snapshot_and_stays_held(env):
    sf, service = env
    service.register_issue(CorporateActionIssueRequest(**command(instance_id="live_v713", symbol="600000.SH", action_type="SPLIT")))
    snapshot(sf, at=(NOW - timedelta(minutes=6)).isoformat())
    req = CorporateActionResolutionRequest(**(command(instance_id="live_v713", issue_id="e1",
        expected_quantity=1000, quantity_delta=100, expected_cash="20000", broker_snapshot_id="snapshot") | {"event_id": "resolve"}))
    with pytest.raises(APIError, match="fresh zoned"):
        service.resolve_issue(req)
    snapshot(sf, identifier="fresh", quantity=2000)
    with pytest.raises(APIError, match="exceeds physically"):
        service.resolve_issue(req.model_copy(update={"broker_snapshot_id": "fresh"}))
    with sf() as s:
        assert s.get(InstanceState, "live_v713").strategy_state["accounting_hold"]
        assert s.get(InstanceState, "live_v713").virtual_positions == {"600000.SH": 1000}


def test_dividend_issue_cannot_bypass_entitlement_income_matching(env):
    sf, service = env
    service.register_issue(CorporateActionIssueRequest(**command(instance_id="live_v713", symbol="600000.SH", action_type="DIVIDEND")))
    snapshot(sf, cash=40100)
    req = CorporateActionResolutionRequest(**(command(instance_id="live_v713", issue_id="e1",
        expected_quantity=1000, cash_delta="100", expected_cash="20000", broker_snapshot_id="snapshot") | {"event_id": "resolve"}))
    with pytest.raises(APIError, match="entitlement matching"):
        service.resolve_issue(req)
    with sf() as s:
        assert s.query(CashFlowJournal).count() == 0


def test_capital_transfer_api_is_operator_scoped(env, settings_for_test):
    from fastapi.testclient import TestClient
    from app.main import create_app
    sf, _ = env
    settings = settings_for_test.model_copy(update={"db_url": str(sf.kw["bind"].url),
        "live_api_key": "ACCOUNT_LIVE", "oms_operator_api_key": "ACCOUNT_OPERATOR",
        "live_client_id": "agent", "live_account_aliases_csv": "shared"})
    client = TestClient(create_app(settings_override=settings))
    payload = CapitalTransferRequest(**command(from_instance_id="hydra", to_instance_id="live_v713", amount="100")).model_dump(mode="json")
    rejected = client.post("/accounts/capital-transfers", json=payload,
        headers={"Authorization": "Bearer ACCOUNT_LIVE"})
    assert rejected.status_code == 403
    accepted = client.post("/accounts/capital-transfers", json=payload,
        headers={"Authorization": "Bearer ACCOUNT_OPERATOR"})
    assert accepted.status_code == 200 and accepted.json()["code"] == 0, accepted.text
    wrong = client.post("/accounts/capital-transfers", json=payload | {"account_alias": "another"},
        headers={"Authorization": "Bearer ACCOUNT_OPERATOR"})
    assert wrong.status_code == 403
