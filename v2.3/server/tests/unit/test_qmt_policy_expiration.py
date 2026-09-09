"""Approved day expiry reaches final close, actual-position residual and capital."""
from __future__ import annotations

import pytest
from types import SimpleNamespace
from sqlalchemy import select

from app.exceptions import APIError
from app.models import (
    HydraExecutionAttempt, HydraRebalance, InstanceState, Order, OrderStatusEvidence, Trade,
)
from app.schemas.hydra_relay import HydraAttemptCloseRequest, HydraRetryRequest
from app.schemas.trade_result import TradeResult
from app.services.hydra_closure import unresolved_orders
from app.services.settlement import SettlementService
from app.services.strategy_capital import BROKER_TERMINAL, StrategyCapitalService
from tests.unit.test_hydra_relay import _setup, _target, _install, _price_frame


def _live(tmp_path):
    service, sf, store, model, raw, actions, calendar = _setup(
        tmp_path, live_enabled=True, state_domain="live",
    )
    first = service.stage_initial(_target(
        model, raw, actions, calendar, execution_domain="live",
        account_alias="hydra-live", instance_id="live_hydra",
    ))
    with sf() as session:
        orders = list(session.scalars(select(Order).order_by(Order.symbol)))
    return service, sf, store, first, orders


def _expired(order, qty=0, **changes):
    payload = dict(
        order_id=order.order_id, filled_quantity=qty,
        filled_price=order.limit_price if qty else 0.0,
        status="EXPIRED_BY_POLICY", symbol=order.symbol, direction=order.direction,
        qmt_order_id="qmt-" + order.order_id, raw_qmt_status=50,
        status_observed_at="2026-08-03T15:00:00+08:00",
        expiration_policy_id="QMT_DAY_ORDER_1500_V1",
    )
    payload.update(changes)
    return TradeResult(**payload)


def _close(service, sf, first, evidence="a" * 64):
    with sf() as session:
        state = session.get(InstanceState, "live_hydra")
        cash, positions = state.virtual_cash, dict(state.virtual_positions)
    return service.close_attempt(HydraAttemptCloseRequest(
        execution_domain="live", account_alias="hydra-live", attempt_id=first.attempt_id,
        actual_cash=cash, actual_positions=positions,
        reconciliation_evidence_sha256=evidence,
    ))


def _retry(service, sf, store, first):
    raw = _install(store, "hydra_execution_raw", _price_frame("20260803"), "none", "20260803")
    with sf() as session:
        state = session.get(InstanceState, "live_hydra")
        cash, positions = state.virtual_cash, dict(state.virtual_positions)
    return service.stage_retry(HydraRetryRequest(
        execution_domain="live", account_alias="hydra-live", rebalance_id=first.rebalance_id,
        trade_date="20260804", execution_raw_sha256=raw,
        actual_cash=cash, actual_positions=positions,
        reconciliation_evidence_sha256="b" * 64,
    ))


def test_expired_partial_fact_close_and_server_residual_end_to_end(tmp_path):
    service, sf, store, first, orders = _live(tmp_path)
    results = [_expired(row, row.quantity - 100) for row in orders]
    settlement = SettlementService(sf)
    reply = settlement.settle("20260803", results, "live", ("hydra-live",))
    assert reply.matched_count == 2
    assert not reply.rejected_observations
    with sf() as session:
        original_cash = session.get(InstanceState, "live_hydra").virtual_cash
        evidence = list(session.scalars(select(OrderStatusEvidence)))
        assert len(evidence) == 2
        assert evidence[0].payload["observation"]["raw_qmt_status"] == 50
        assert evidence[0].payload["batch_sha256"] == first.batch_sha256
        assert not unresolved_orders(session, first.rebalance_id)
        assert "EXPIRED_BY_POLICY" in BROKER_TERMINAL
        assert StrategyCapitalService(sf)._protected_buy_cash(session, SimpleNamespace(
            execution_domain="live", account_alias="hydra-live", instance_id="live_hydra",
        )) == 0
    # Same cumulative fill/status replay must not book again or duplicate evidence.
    assert settlement.settle("20260803", results, "live").matched_count == 0
    with sf() as session:
        assert session.get(InstanceState, "live_hydra").virtual_cash == original_cash
        assert len(list(session.scalars(select(OrderStatusEvidence)))) == 2
    closed = _close(service, sf, first)
    assert closed.status == "RESIDUAL"
    assert closed.effective_finalized is True
    assert closed.broker_finalized is False
    assert closed.retry_ready is True
    assert closed.residual_after == {order.symbol: 100 for order in orders}
    assert _close(service, sf, first) == closed
    retry = _retry(service, sf, store, first)
    with sf() as session:
        delta = list(session.scalars(select(Order).where(Order.attempt_id == retry.attempt_id)))
        assert {row.symbol: row.quantity for row in delta} == closed.residual_after


@pytest.mark.parametrize("changes", [
    {"status_observed_at": "2026-08-03T14:59:59+08:00"},
    {"status_observed_at": "2026-08-04T15:00:00+08:00"},
    {"status_observed_at": "2026-08-03T15:00:00"},
    {"status_observed_at": None},
    {"expiration_policy_id": "UNAPPROVED"},
    {"raw_qmt_status": 55},
    {"qmt_order_id": None},
    {"symbol": "wrong"},
    {"direction": "SELL"},
])
def test_invalid_expiry_does_not_discard_other_valid_facts(tmp_path, changes):
    _, sf, _, _, orders = _live(tmp_path)
    bad, good = orders
    response = SettlementService(sf).settle("20260803", [
        _expired(bad, **changes), _expired(good),
    ], "live", ("hydra-live",))
    assert response.matched_count == 1
    assert set(response.rejected_observations) == {bad.order_id}
    with sf() as session:
        assert session.get(Order, bad.order_id).status == "PENDING"
        assert session.get(Order, good.order_id).status == "EXPIRED_BY_POLICY"


@pytest.mark.parametrize("field,value", [
    ("valid_date", "20260804"), ("batch_sha256", "f" * 64),
    ("target_id", None), ("attempt_id", "missing"), ("qmt_account_alias", "other"),
])
def test_expiry_bound_to_server_order_and_authorized_account(tmp_path, field, value):
    _, sf, _, _, orders = _live(tmp_path)
    order = orders[0]
    with sf() as session:
        setattr(session.get(Order, order.order_id), field, value)
        session.commit()
    response = SettlementService(sf).settle("20260803", [_expired(order)], "live", ("hydra-live",))
    assert response.matched_count == 0
    assert response.rejected_observations or response.unmatched_order_ids


def test_same_quantity_active_observation_cannot_resurrect_policy_expiry(tmp_path):
    _, sf, _, _, orders = _live(tmp_path)
    order = orders[0]
    svc = SettlementService(sf)
    svc.settle("20260803", [_expired(order, 100)], "live")
    response = svc.settle("20260803", [TradeResult(
        order_id=order.order_id, filled_quantity=100, filled_price=order.limit_price,
        status="PARTIAL",
    )], "live")
    assert response.matched_count == 0
    with sf() as session:
        assert session.get(Order, order.order_id).status == "EXPIRED_BY_POLICY"


def test_late_fill_reopens_old_close_and_holds_existing_successor(tmp_path):
    service, sf, store, first, orders = _live(tmp_path)
    svc = SettlementService(sf)
    svc.settle("20260803", [_expired(row) for row in orders], "live")
    _close(service, sf, first)
    retry = _retry(service, sf, store, first)
    old = orders[0]
    response = svc.settle("20260803", [_expired(old, 100)], "live")
    assert response.matched_count == 1
    assert response.local_batch_review_required
    assert set(response.invalidated_attempt_ids) == {first.attempt_id, retry.attempt_id}
    replay = svc.settle("20260803", [_expired(old, 100)], "live")
    assert replay.matched_count == 0
    assert replay.local_batch_review_required
    with sf() as session:
        assert session.get(InstanceState, "live_hydra").virtual_positions[old.symbol] == 100
        assert session.get(HydraRebalance, first.rebalance_id).status != "COMPLETED"
        for attempt_id in (first.attempt_id, retry.attempt_id):
            attempt = session.get(HydraExecutionAttempt, attempt_id)
            assert attempt.status == "CLOSED_PENDING_RECONCILIATION"
            assert attempt.risk_snapshot["late_fill_review"]["requires_manual_resolution"]
        assert len(list(session.scalars(select(Trade).where(Trade.order_id == old.order_id)))) == 2
    with pytest.raises(APIError, match="迟到成交"):
        _close(service, sf, first, "c" * 64)


def test_late_fill_without_successor_can_reconcile_again(tmp_path):
    service, sf, _, first, orders = _live(tmp_path)
    svc = SettlementService(sf)
    svc.settle("20260803", [_expired(row) for row in orders], "live")
    original = _close(service, sf, first)
    svc.settle("20260803", [_expired(orders[0], 100)], "live")
    revised = _close(service, sf, first, "c" * 64)
    assert revised.residual_after[orders[0].symbol] == original.residual_after[orders[0].symbol] - 100


def test_late_active_partial_keeps_expiry_but_books_real_fill(tmp_path):
    service, sf, _, first, orders = _live(tmp_path)
    svc = SettlementService(sf)
    svc.settle("20260803", [_expired(row) for row in orders], "live")
    original = _close(service, sf, first)
    order = orders[0]
    svc.settle("20260803", [TradeResult(
        order_id=order.order_id, filled_quantity=100, filled_price=order.limit_price,
        status="PARTIAL", qmt_order_id="qmt-" + order.order_id,
    )], "live")
    with sf() as session:
        assert session.get(Order, order.order_id).status == "EXPIRED_BY_POLICY"
        assert session.get(InstanceState, "live_hydra").virtual_positions[order.symbol] == 100
        assert session.get(HydraExecutionAttempt, first.attempt_id).status == "CLOSED_PENDING_RECONCILIATION"
    revised = _close(service, sf, first, "c" * 64)
    assert revised.residual_after[order.symbol] == original.residual_after[order.symbol] - 100
