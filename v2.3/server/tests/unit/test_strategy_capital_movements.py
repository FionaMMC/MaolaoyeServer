"""Capital movements preserve ownership, unresolved obligations and NAV meaning."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.db import init_db, make_engine, make_session_factory
from app.exceptions import APIError
from app.main import create_app
from app.models import CashFlowJournal, InstanceState, Order, OrderSignalMap, PerfSnapshot, RawSignal, Trade
from app.models.capital_movement_receipt import CapitalMovementReceipt
from app.models.account_cash_observation import AccountCashObservation
from app.schemas.cash_flow import CashFlowRequest
from app.schemas.strategy_capital import CapitalMovementRequest
from app.services.cash_flow import CashFlowService
from app.services.perf import PerfService
from app.services.strategy_capital import StrategyCapitalService


@pytest.fixture
def capital(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/capital.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as session:
        session.add(InstanceState(
            instance_id="hydra", execution_domain="paper", account_alias="test-account",
            ledger_mode="attributed", virtual_cash=1000.0,
            virtual_positions={"ETF": 200}, owned_symbols=["ETF"],
            strategy_state={"initial_allocated_cash": 1000, "model_day": 17},
            last_update="2026-09-07T18:00:00+08:00",
        ))
        session.commit()
    yield StrategyCapitalService(sf), sf
    engine.dispose()


def request(**changes):
    payload = dict(
        execution_domain="paper", account_alias="test-account", instance_id="hydra",
        action="INCREASE", amount="500.00", event_date="20260908",
        qmt_cash_balance="10000.00", snapshot_time="2026-09-08T15:30:00+08:00",
        source_event_id="capital-1", evidence_sha256="a" * 64,
    )
    payload.update(changes)
    return CapitalMovementRequest(**payload)


def pending_order(sf, *, status="PENDING", quantity=100, filled=0, divergence=False):
    with sf() as session:
        session.add(Order(
            order_id="order-1", execution_domain="paper", qmt_account_alias="test-account",
            account_group="hydra", symbol="ETF", direction="BUY", quantity=quantity,
            limit_price=5, valid_date="20260908", status=status,
            created_at="2026-09-07T18:00:00+08:00", bookkeeping_divergence=divergence,
        ))
        session.add(RawSignal(
            signal_id="signal-1", execution_domain="paper", instance_id="hydra",
            symbol="ETF", direction="BUY", quantity=quantity, reference_price=5,
            price_offset=0, limit_price=5, valid_date="20260908",
            signal_time="2026-09-07T18:00:00+08:00", precheck_status="PASS",
        ))
        session.add(OrderSignalMap(order_id="order-1", signal_id="signal-1", signal_quantity=quantity))
        if filled:
            session.add(Trade(
                order_id="order-1", execution_domain="paper", filled_quantity=filled,
                filled_price=5, filled_time="2026-09-08T09:35:00+08:00",
                status="PARTIAL", received_at="2026-09-08T15:30:00+08:00",
            ))
        session.commit()


def test_increase_is_delta_and_keeps_profit_positions_and_strategy_state(capital):
    service, sf = capital
    with sf() as session:
        session.get(InstanceState, "hydra").virtual_cash = 1100  # already-earned cash
        session.commit()
    receipt = service.apply(request())
    assert receipt.amount == 500
    assert receipt.target_policy == "NEXT_UNFROZEN_TARGET"
    with sf() as session:
        state = session.get(InstanceState, "hydra")
        assert state.virtual_cash == 1600
        assert state.virtual_positions == {"ETF": 200}
        assert state.strategy_state == {"initial_allocated_cash": 1000, "model_day": 17}
        assert session.query(Order).count() == 0
        row = session.get(CashFlowJournal, receipt.journal_id)
        audit = session.get(CapitalMovementReceipt, receipt.journal_id)
        assert row.evidence_sha256 == "a" * 64
        assert audit.request_payload["evidence_sha256"] == "a" * 64
        assert audit.request_sha256 == receipt.request_sha256


def test_no_set_balance_or_implicit_initialization(capital):
    service, _ = capital
    with pytest.raises(ValidationError):
        request(action="SET_BALANCE")
    with pytest.raises(ValidationError):
        request(target_cash=2000)
    with pytest.raises(APIError, match="初始化"):
        service.apply(request(instance_id="not-created"))


@pytest.mark.parametrize("changes", [
    {"amount": "0"}, {"amount": "-1"}, {"amount": "NaN"},
    {"amount": "0.001"}, {"event_date": "20260230"},
    {"snapshot_time": "2026-09-08T15:30:00"},
])
def test_request_rejects_ambiguous_money_and_time(changes):
    with pytest.raises(ValidationError):
        request(**changes)


def test_replay_returns_original_receipt_before_current_resource_checks(capital):
    service, sf = capital
    first = service.apply(request())
    with sf() as session:
        session.get(InstanceState, "hydra").virtual_cash = 20_000
        session.commit()
    replay = service.apply(request(amount="500.0"))
    assert replay.already_applied
    assert first.model_dump(exclude={"already_applied"}) == replay.model_dump(exclude={"already_applied"})
    with sf() as session:
        assert session.get(InstanceState, "hydra").virtual_cash == 20_000
        assert session.query(CashFlowJournal).count() == 1


@pytest.mark.parametrize("changes", [
    {"amount": "501"}, {"qmt_cash_balance": "20000"},
    {"evidence_sha256": "b" * 64}, {"description": "different"},
])
def test_source_event_cannot_be_rewritten(capital, changes):
    service, _ = capital
    service.apply(request())
    with pytest.raises(APIError) as error:
        service.apply(request(**changes))
    assert error.value.http_status == 409


def test_no_cross_domain_or_account_transfer(capital):
    service, _ = capital
    for changes in ({"execution_domain": "live"}, {"account_alias": "another-account"}):
        with pytest.raises(APIError) as error:
            service.apply(request(**changes))
        assert error.value.http_status == 403


def test_cannot_use_other_strategy_cash_but_other_account_is_independent(capital):
    service, sf = capital
    with sf() as session:
        for instance, alias, cash in [("peer", "test-account", 8500), ("elsewhere", "other", 1e6)]:
            session.add(InstanceState(
                instance_id=instance, execution_domain="paper", account_alias=alias,
                ledger_mode="attributed", virtual_cash=cash, virtual_positions={}, last_update="now",
            ))
        session.commit()
    with pytest.raises(APIError, match="未分配现金不足"):
        service.apply(request(amount=501))
    assert service.apply(request()).amount == 500


@pytest.mark.parametrize("status", ["PENDING", "PARTIAL", "REPORTED", "EXPIRED_BY_TIME"])
def test_decrease_keeps_pending_buy_budget_and_fee_even_after_workflow_expiry(capital, status):
    service, sf = capital
    pending_order(sf, status=status)
    with pytest.raises(APIError, match="未占用现金"):
        service.apply(request(action="DECREASE", amount=496))
    service.apply(request(action="DECREASE", amount=495))
    with sf() as session:
        assert session.get(InstanceState, "hydra").virtual_cash == 505
        assert session.get(Order, "order-1").status == status


def test_partial_cumulative_fills_reduce_obligation_without_double_count(capital):
    service, sf = capital
    pending_order(sf, status="PARTIAL", filled=40)
    with sf() as session:
        session.add(Trade(
            order_id="order-1", execution_domain="paper", filled_quantity=20,
            filled_price=5, filled_time="earlier", status="PARTIAL", received_at="earlier",
        ))
        session.get(InstanceState, "hydra").virtual_cash = 795
        session.commit()
    with pytest.raises(APIError):
        service.apply(request(action="DECREASE", amount=491))
    service.apply(request(action="DECREASE", amount=490))
    with sf() as session:
        assert session.get(InstanceState, "hydra").virtual_cash == 305


@pytest.mark.parametrize("status", ["FILLED", "CANCELLED", "REJECTED", "NOT_SUBMITTED"])
def test_confirmed_terminal_order_does_not_freeze_released_cash(capital, status):
    service, sf = capital
    pending_order(sf, status=status)
    service.apply(request(action="DECREASE", amount=1000))
    with sf() as session:
        assert session.get(InstanceState, "hydra").virtual_cash == 0


def test_bookkeeping_divergence_cannot_be_hidden_by_terminal_state(capital):
    service, sf = capital
    pending_order(sf, status="FILLED", divergence=True)
    with pytest.raises(APIError, match="账务差异"):
        service.apply(request(action="DECREASE", amount=1))


def test_increase_does_not_claim_other_owners_unposted_sell_proceeds(capital):
    service, sf = capital
    pending_order(sf, status="FILLED", divergence=True)
    with sf() as session:
        session.get(Order, "order-1").direction = "SELL"
        session.get(RawSignal, "signal-1").instance_id = "another-owner"
        session.commit()
    with pytest.raises(APIError, match="卖出所得"):
        service.apply(request(amount=500))


def test_increase_does_not_wait_for_old_order_to_close(capital):
    service, sf = capital
    pending_order(sf)
    service.apply(request())
    with sf() as session:
        assert session.get(Order, "order-1").quantity == 100
        assert session.get(InstanceState, "hydra").virtual_cash == 1500


def test_legacy_capital_decrease_cannot_bypass_pending_order_protection(capital):
    _, sf = capital
    pending_order(sf)
    req = CashFlowRequest(
        execution_domain="paper", account_alias="test-account", instance_id="hydra",
        event_date="20260908", event_type="CAPITAL_DEALLOCATION", amount=-600,
        qmt_cash=10000, snapshot_time="2026-09-08T15:30:00+08:00",
        source="legacy-client", source_event_id="legacy-decrease-1", evidence_sha256="b" * 64,
    )
    with pytest.raises(APIError, match="保护现金"):
        CashFlowService(sf).apply(req)


def test_legacy_allocation_cannot_bypass_physical_event_watermark(capital):
    _, sf = capital
    add_observation(sf, recorded_at="2026-09-08T08:00:00Z")
    req = CashFlowRequest(
        execution_domain="paper", account_alias="test-account", instance_id="hydra",
        event_date="20260908", event_type="CAPITAL_ALLOCATION", amount=500,
        qmt_cash=10000, snapshot_time="2026-09-08T15:30:00+08:00",
        source="legacy-client", source_event_id="legacy-increase-1", evidence_sha256="b" * 64,
    )
    with pytest.raises(APIError, match="现金快照早于"):
        CashFlowService(sf).apply(req)


def test_negative_peer_cash_is_not_free_reserve(capital):
    service, sf = capital
    with sf() as session:
        session.add(InstanceState(
            instance_id="peer", execution_domain="paper", account_alias="test-account",
            ledger_mode="attributed", virtual_cash=-500, virtual_positions={}, last_update="now",
        ))
        session.commit()
    with pytest.raises(APIError, match="未分配现金不足"):
        service.apply(request(qmt_cash_balance=1000, amount=1))


def test_increase_rejects_old_snapshot_after_intervening_buy_fill(capital):
    service, sf = capital
    pending_order(sf, status="FILLED", filled=100)
    with sf() as session:
        session.get(InstanceState, "hydra").virtual_cash = 500
        session.commit()
    with pytest.raises(APIError, match="现金快照早于"):
        service.apply(request(amount=9500, snapshot_time="2026-09-08T15:00:00+08:00"))
    with sf() as session:
        assert session.get(InstanceState, "hydra").virtual_cash == 500
        assert session.query(CashFlowJournal).count() == 0


def add_observation(sf, *, recorded_at, account="test-account", domain="paper",
                    event_type="WITHDRAWAL", amount=-500):
    with sf() as session:
        session.add(AccountCashObservation(
            execution_domain=domain, account_alias=account, source="qmt", source_event_id="withdrawal",
            event_type=event_type, amount=amount, observed_at=recorded_at,
            evidence_sha256="a" * 64, request_sha256="b" * 64,
            payload={}, recorded_at=recorded_at,
        ))
        session.commit()


def test_newer_account_withdrawal_requires_fresh_snapshot_but_replay_still_succeeds(capital):
    service, sf = capital
    first = service.apply(request())
    add_observation(sf, recorded_at="2026-09-08T07:31:00Z")
    assert service.apply(request()).journal_id == first.journal_id
    with pytest.raises(APIError, match="现金快照早于"):
        service.apply(request(source_event_id="new-increase"))
    service.apply(request(source_event_id="new-increase", snapshot_time="2026-09-08T15:32:00+08:00"))


def test_snapshot_watermark_compares_offsets_as_instants(capital):
    service, sf = capital
    add_observation(sf, recorded_at="2026-09-08T08:00:00+01:00")  # 07:00 UTC
    service.apply(request(snapshot_time="2026-09-08T15:00:00+08:00"))  # same instant


def test_internal_capital_events_do_not_invalidate_same_physical_snapshot(capital):
    service, _ = capital
    service.apply(request())
    service.apply(request(source_event_id="second-increase"))


def test_other_account_events_do_not_block_our_capital_change(capital):
    service, sf = capital
    add_observation(sf, recorded_at="2099-01-01T00:00:00Z", account="another-account")
    service.apply(request())


@pytest.mark.parametrize("event_type", ["DIVIDEND", "INTEREST", "OTHER"])
def test_unattributed_positive_income_is_not_capital_reserve(capital, event_type):
    service, sf = capital
    add_observation(sf, recorded_at="2026-09-08T07:00:00Z", event_type=event_type, amount=100)
    with pytest.raises(APIError, match="未分配现金不足"):
        service.apply(request(qmt_cash_balance=1100, amount=100))


def test_income_suspense_does_not_block_existing_unallocated_reserve(capital):
    service, sf = capital
    add_observation(sf, recorded_at="2026-09-08T07:00:00Z", event_type="DIVIDEND", amount=100)
    service.apply(request(qmt_cash_balance=2100, amount=1000))
    with sf() as session:
        assert session.get(InstanceState, "hydra").virtual_cash == 2000


def test_normal_deposit_is_not_income_suspense(capital):
    service, sf = capital
    add_observation(sf, recorded_at="2026-09-08T07:00:00Z", event_type="DEPOSIT", amount=100)
    service.apply(request(qmt_cash_balance=1100, amount=100))


def test_legacy_allocation_cannot_claim_unattributed_income(capital):
    _, sf = capital
    add_observation(sf, recorded_at="2026-09-08T07:00:00Z", event_type="DIVIDEND", amount=100)
    req = CashFlowRequest(
        execution_domain="paper", account_alias="test-account", instance_id="hydra",
        event_date="20260908", event_type="CAPITAL_ALLOCATION", amount=100,
        qmt_cash=1100, snapshot_time="2026-09-08T15:30:00+08:00",
        source="legacy-client", source_event_id="legacy-increase-1", evidence_sha256="b" * 64,
    )
    with pytest.raises(APIError):
        CashFlowService(sf).apply(req)


def test_parallel_movements_do_not_overallocate_same_snapshot(capital):
    service, sf = capital
    def run(index):
        try:
            service.apply(request(source_event_id=f"parallel-{index}", qmt_cash_balance=1500, amount=400))
            return "applied"
        except APIError as error:
            assert error.http_status == 409
            return "no-free-cash"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, range(2)))
    assert sorted(results) == ["applied", "no-free-cash"]
    with sf() as session:
        assert session.get(InstanceState, "hydra").virtual_cash == 1400


def test_parallel_replay_and_legacy_cash_flow_preserve_both_deltas(capital, monkeypatch):
    service, sf = capital
    # Delivery is concurrent, but the mocked server receipt predates the QMT
    # snapshot used in this concurrency-only test (freshness tested separately).
    monkeypatch.setattr("app.services.cash_flow._now_iso", lambda: "2026-09-08T07:00:00Z")
    legacy = CashFlowService(sf)
    dividend = CashFlowRequest(
        execution_domain="paper", account_alias="test-account", instance_id="hydra",
        event_date="20260908", event_type="DIVIDEND", amount=12.34,
        source="qmt", source_event_id="dividend-1", evidence_sha256="c" * 64,
    )
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = [executor.submit(service.apply, request()), executor.submit(service.apply, request()),
                   executor.submit(legacy.apply, dividend)]
        for result in results:
            result.result()
    with sf() as session:
        assert session.get(InstanceState, "hydra").virtual_cash == 1512.34
        assert session.query(CashFlowJournal).count() == 2


@pytest.mark.parametrize("requested_date", ["20000101", "20991231"])
def test_immediate_capital_command_uses_apply_date_not_requested_date(capital, requested_date):
    service, sf = capital
    receipt = service.apply(request(event_date=requested_date))
    effective_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    assert receipt.effective_date == effective_date
    with sf() as session:
        journal = session.get(CashFlowJournal, receipt.journal_id)
        audit = session.get(CapitalMovementReceipt, receipt.journal_id)
        assert journal.event_date == effective_date
        assert audit.request_payload["event_date"] == requested_date


def test_shared_aggregate_order_cannot_underreserve_owner_after_rounded_fills(capital):
    service, sf = capital
    pending_order(sf, quantity=100, status="PARTIAL", filled=40)
    with sf() as session:
        session.get(OrderSignalMap, ("order-1", "signal-1")).signal_quantity = 50
        session.add(RawSignal(
            signal_id="peer-signal", execution_domain="paper", instance_id="peer",
            symbol="ETF", direction="BUY", quantity=50, reference_price=5,
            price_offset=0, limit_price=5, valid_date="20260908",
            signal_time="2026-09-07T18:00:00+08:00", precheck_status="PASS",
        ))
        session.add(OrderSignalMap(order_id="order-1", signal_id="peer-signal", signal_quantity=50))
        session.commit()
    # Pro-rata would hold only 30 * 5 + 5; current compatibility bound keeps
    # 60 * 5 + 5 because historical per-delta rounding is not owner-persisted.
    with pytest.raises(APIError, match="未占用现金"):
        service.apply(request(action="DECREASE", amount=696))
    service.apply(request(action="DECREASE", amount=695))


def test_live_capital_api_changes_only_owned_cash_with_trading_switches_closed(capital, settings_for_test):
    _, sf = capital
    with sf() as session:
        session.get(InstanceState, "hydra").execution_domain = "live"
        session.commit()
    settings = settings_for_test.model_copy(update={
        "db_url": str(sf.kw["bind"].url), "live_api_key": "CAPITAL_TEST_KEY",
        "live_client_id": "test-client", "live_account_aliases_csv": "test-account",
        "live_cash_flow_ingest_enabled": False, "live_account_initialization_enabled": False,
        "live_order_generation_enabled": False, "live_order_delivery_enabled": False,
    })
    client = TestClient(create_app(settings_override=settings))
    response = client.post("/accounts/capital-movements",
                           json=request(execution_domain="live").model_dump(mode="json"),
                           headers={"Authorization": "Bearer CAPITAL_TEST_KEY"})
    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0
    assert response.json()["data"]["amount"] == 500
    assert response.json()["data"]["effective_date"] == datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    with sf() as session:
        assert session.get(InstanceState, "hydra").virtual_cash == 1500
        assert session.query(Order).count() == 0


@pytest.mark.parametrize("key,changes,expected", [
    (None, {}, 401), ("CAPITAL_TEST_KEY", {"account_alias": "other"}, 403),
    ("CAPITAL_TEST_KEY", {"execution_domain": "paper"}, 403),
    ("TEST_KEY", {}, 403),
])
def test_capital_api_retains_domain_account_auth(capital, settings_for_test, key, changes, expected):
    _, sf = capital
    settings = settings_for_test.model_copy(update={
        "db_url": str(sf.kw["bind"].url), "live_api_key": "CAPITAL_TEST_KEY",
        "live_client_id": "test-client", "live_account_aliases_csv": "test-account",
    })
    client = TestClient(create_app(settings_override=settings))
    payload = {"execution_domain": "live"} | changes
    response = client.post("/accounts/capital-movements", json=request(**payload).model_dump(mode="json"),
                           headers={"Authorization": f"Bearer {key}"} if key else {})
    assert response.status_code == expected, response.text
    with sf() as session:
        assert session.query(CashFlowJournal).count() == 0


@pytest.mark.parametrize("event_type, amount, nav, expected", [
    ("CAPITAL_ALLOCATION", 500, 1500, 0),
    ("CAPITAL_DEALLOCATION", -400, 600, 0),
    ("DEPOSIT", 500, 1530, .03),
    ("WITHDRAWAL", -400, 580, -.02),
    ("DIVIDEND", 50, 1050, .05),
    ("OTHER", -10, 990, -.01),
])
def test_return_excludes_capital_not_investment_income(capital, event_type, amount, nav, expected):
    _, sf = capital
    with sf() as session:
        session.add(PerfSnapshot(instance_id="hydra", execution_domain="paper", date="20260907",
                                 nav=1000, daily_return=None, positions_snapshot={}))
        session.add(CashFlowJournal(
            execution_domain="paper", account_alias="test-account", instance_id="hydra",
            event_date="20260908", event_type=event_type, amount=amount, currency="CNY",
            source="test", source_event_id="flow", evidence_sha256="a" * 64,
            status="APPLIED", created_at="now", applied_at="now",
        ))
        session.commit()
        assert PerfService(sf, None)._compute_daily_return(session, "hydra", "20260908", nav) == expected


def test_return_flow_window_and_domain_are_isolated(capital):
    _, sf = capital
    with sf() as session:
        session.add(PerfSnapshot(instance_id="hydra", execution_domain="paper", date="20260905",
                                 nav=1000, daily_return=None, positions_snapshot={}))
        for index, (date, domain, instance, status, amount) in enumerate([
            ("20260905", "paper", "hydra", "APPLIED", 999),
            ("20260906", "paper", "hydra", "APPLIED", 100),
            ("20260907", "paper", "hydra", "APPLIED", 200),
            ("20260908", "live", "hydra", "APPLIED", 999),
            ("20260908", "paper", "other", "APPLIED", 999),
            ("20260908", "paper", "hydra", "PENDING", 999),
            ("20260909", "paper", "hydra", "APPLIED", 999),
        ]):
            session.add(CashFlowJournal(
                execution_domain=domain, account_alias="test-account", instance_id=instance,
                event_date=date, event_type="CAPITAL_ALLOCATION", amount=amount, currency="CNY",
                source="test", source_event_id=str(index), evidence_sha256="a" * 64,
                status=status, created_at="now", applied_at="now",
            ))
        session.commit()
        assert PerfService(sf, None)._compute_daily_return(session, "hydra", "20260908", 1310) == .01
