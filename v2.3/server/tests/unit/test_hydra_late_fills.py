"""A positive late fill invalidates stale close snapshots, not broker evidence."""
from copy import deepcopy

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import (
    HydraExecutionAttempt, HydraRebalance, HydraTarget, Order,
    OrderSignalMap, RawSignal, Trade,
)
from app.services.hydra_late_fills import (
    invalidate_hydra_close_after_late_fill, unresolved_late_fill_review,
)
from app.services.orders_queue import OrdersQueueService

RECEIVED = "2026-09-09T07:10:00+00:00"


def _attempt(number, status, **changes):
    values = dict(
        attempt_id=f"attempt-{number}", rebalance_id="rebalance", execution_domain="live",
        account_alias="live-test", attempt_number=number, trade_date="20260909",
        residual_before={"ETF": 100}, residual_after={"ETF": 40},
        posttrade_reconciliation_sha256="a" * 64, reconciled_cash=600,
        reconciled_positions={"ETF": 60}, risk_snapshot={"risk_limit": 1000},
        batch_id=f"batch-{number}", batch_sha256=str(number) * 64,
        status=status, created_at="2026-09-08T10:00:00+00:00",
        closed_at="2026-09-09T07:00:00+00:00",
    )
    values.update(changes)
    return HydraExecutionAttempt(**values)


def _order(number, **changes):
    values = dict(
        order_id=f"order-{number}", execution_domain="live", qmt_account_alias="live-test",
        target_id="target", rebalance_id="rebalance", attempt_id=f"attempt-{number}",
        attempt_number=number, batch_id=f"batch-{number}", batch_sha256=str(number) * 64,
        target_hash="c" * 64, account_group="live-test", symbol="ETF", direction="BUY",
        quantity=100, limit_price=5, valid_date="20260909", status="EXPIRED_BY_POLICY",
        fetched_at="2026-09-08T11:00:00+00:00", created_at="2026-09-08T10:00:00+00:00",
        bookkeeping_divergence=False,
    )
    values.update(changes)
    return Order(**values)


def _fill(session, quantity=20, received_at=RECEIVED):
    session.add(Trade(
        order_id="order-1", execution_domain="live", filled_quantity=quantity,
        filled_price=5, filled_time="2026-09-09T06:59:00+00:00", status="PARTIAL",
        received_at=received_at,
    ))


def _map_owner(session, order_id, owner, domain="live"):
    signal_id = f"signal-{order_id}"
    session.add(RawSignal(
        signal_id=signal_id, execution_domain=domain, instance_id=owner,
        symbol="ETF", direction="BUY", quantity=100, reference_price=5,
        price_offset=0, limit_price=5, valid_date="20260909",
        signal_time="2026-09-08T10:00:00+00:00", precheck_status="PASS",
    ))
    session.add(OrderSignalMap(order_id=order_id, signal_id=signal_id, signal_quantity=100))


def _other_cycle(session, number, owner, *, trade_date="20261009",
                 created_at="2026-10-08T10:00:00+00:00", fetched=False,
                 account="live-test", domain="live", orderless=False):
    target_id, rebalance_id = f"target-{number}", f"rebalance-{number}"
    session.add(HydraTarget(
        target_id=target_id, execution_domain=domain, account_alias=account,
        strategy_version="hydra-test", publisher_source_commit="d" * 40,
        decision_date=trade_date, as_of_date=trade_date, execution_date=trade_date,
        basket_sha256=str(number) * 64, research_input_hashes={}, input_hashes={},
        weights={"ETF": 1.0}, cash_buffer_weight=0, status="STAGED", created_at=created_at,
    ))
    session.add(HydraRebalance(
        rebalance_id=rebalance_id, target_id=target_id, execution_domain=domain,
        account_alias=account, baseline_cash=600, baseline_positions={"ETF": 60},
        target_shares={"ETF": 200}, status="OPEN", reconciliation_status="PRE_TRADE_OK",
        created_at=created_at,
    ))
    session.add(_attempt(
        number, "NOOP" if orderless else "PENDING", attempt_number=1,
        rebalance_id=rebalance_id, account_alias=account, execution_domain=domain,
        trade_date=trade_date, created_at=created_at,
    ))
    if not orderless:
        session.add(_order(
            number, status="PENDING", attempt_number=1, target_id=target_id,
            rebalance_id=rebalance_id, qmt_account_alias=account, execution_domain=domain,
            valid_date=trade_date, fetched_at=created_at if fetched else None,
        ))
        _map_owner(session, f"order-{number}", owner, domain)


@pytest.fixture
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/late-fills.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        session.add(HydraTarget(
            target_id="target", execution_domain="live", account_alias="live-test",
            strategy_version="hydra-test", publisher_source_commit="d" * 40,
            decision_date="20260908", as_of_date="20260908", execution_date="20260909",
            basket_sha256="e" * 64, research_input_hashes={}, input_hashes={},
            weights={"ETF": 1.0}, cash_buffer_weight=0, status="COMPLETED", created_at="before",
        ))
        session.add(HydraRebalance(
            rebalance_id="rebalance", target_id="target", execution_domain="live",
            account_alias="live-test", baseline_cash=1000, baseline_positions={},
            target_shares={"ETF": 100}, status="COMPLETED", reconciliation_status="POST_TRADE_OK",
            created_at="before", closed_at="2026-09-09T07:00:00+00:00",
        ))
        session.add(_attempt(1, "COMPLETE"))
        session.add(_order(1))
        session.commit()
    yield factory
    engine.dispose()


@pytest.mark.parametrize("status", ["COMPLETE", "RESIDUAL"])
def test_final_close_is_reopened_with_original_snapshot_audited(sf, status):
    with sf() as session:
        attempt = session.get(HydraExecutionAttempt, "attempt-1")
        attempt.status = status
        order = session.get(Order, "order-1")
        order_fields = {column.name: getattr(order, column.name) for column in Order.__table__.columns}
        _fill(session)
        assert invalidate_hydra_close_after_late_fill(session, order, RECEIVED) == ["attempt-1"]
        assert attempt.status == "CLOSED_PENDING_RECONCILIATION"
        assert attempt.posttrade_reconciliation_sha256 is None
        assert attempt.residual_after is None
        assert attempt.reconciled_cash is None
        assert attempt.reconciled_positions is None
        review = attempt.risk_snapshot["late_fill_review"]
        old = review["events"][0]["invalidated_close"]
        assert old["status"] == status
        assert old["posttrade_reconciliation_sha256"] == "a" * 64
        assert old["residual_after"] == {"ETF": 40}
        assert old["reconciled_cash"] == 600
        assert not review["requires_manual_resolution"]
        assert attempt.risk_snapshot["risk_limit"] == 1000
        assert {column.name: getattr(order, column.name) for column in Order.__table__.columns} == order_fields
        assert session.get(HydraRebalance, "rebalance").status == "OPEN"
        assert session.get(HydraTarget, "target").status == "ACTIVE"
        session.commit()
    with sf() as session:
        assert session.get(HydraExecutionAttempt, "attempt-1").risk_snapshot["late_fill_review"]["events"]


@pytest.mark.parametrize("status", ["PENDING", "NOOP", "COMPLETE", "RESIDUAL"])
@pytest.mark.parametrize("fetched", [False, True])
def test_successor_is_quarantined_without_rewriting_frozen_order(sf, status, fetched):
    with sf() as session:
        session.add(_attempt(2, status))
        session.add(_order(2, status="PENDING", fetched_at=RECEIVED if fetched else None))
        session.commit()
    with sf() as session:
        _fill(session)
        assert invalidate_hydra_close_after_late_fill(session, session.get(Order, "order-1"), RECEIVED) == ["attempt-1", "attempt-2"]
        for number in (1, 2):
            attempt = session.get(HydraExecutionAttempt, f"attempt-{number}")
            assert attempt.status == "CLOSED_PENDING_RECONCILIATION"
            review = attempt.risk_snapshot["late_fill_review"]
            assert review["requires_manual_resolution"]
            assert review["affected_successor_attempt_ids"] == ["attempt-2"]
            assert review["possibly_delivered_successor_order_ids"] == (["order-2"] if fetched else [])
        successor_order = session.get(Order, "order-2")
        assert successor_order.status == "PENDING"
        assert successor_order.quantity == 100
        assert successor_order.batch_sha256 == "2" * 64
        assert successor_order.fetched_at == (RECEIVED if fetched else None)
        session.commit()
    assert OrdersQueueService(sf).list_pending("20260909", "live", ("live-test",)) == []


def test_same_event_is_idempotent_and_same_second_new_quantity_appends(sf):
    with sf() as session:
        order = session.get(Order, "order-1")
        _fill(session, 20)
        assert invalidate_hydra_close_after_late_fill(session, order, RECEIVED) == ["attempt-1"]
        first = deepcopy(session.get(HydraExecutionAttempt, "attempt-1").risk_snapshot)
        assert invalidate_hydra_close_after_late_fill(session, order, RECEIVED) == []
        assert session.get(HydraExecutionAttempt, "attempt-1").risk_snapshot == first
        _fill(session, 30)
        invalidate_hydra_close_after_late_fill(session, order, RECEIVED)
        events = session.get(HydraExecutionAttempt, "attempt-1").risk_snapshot["late_fill_review"]["events"]
        assert [event["cumulative_filled_quantity"] for event in events] == [20, 30]
        assert len({event["event_id"] for event in events}) == 2
        assert events[0]["invalidated_close"]["status"] == "COMPLETE"


def test_reclosed_attempt_is_invalidated_again_by_new_positive_fill(sf):
    with sf() as session:
        order = session.get(Order, "order-1")
        _fill(session, 20)
        invalidate_hydra_close_after_late_fill(session, order, RECEIVED)
        attempt = session.get(HydraExecutionAttempt, "attempt-1")
        attempt.status = "RESIDUAL"
        attempt.residual_after = {"ETF": 80}
        attempt.posttrade_reconciliation_sha256 = "b" * 64
        _fill(session, 30)
        invalidate_hydra_close_after_late_fill(session, order, RECEIVED)
        assert attempt.status == "CLOSED_PENDING_RECONCILIATION"
        assert attempt.risk_snapshot["late_fill_review"]["events"][1]["invalidated_close"]["posttrade_reconciliation_sha256"] == "b" * 64


def test_bookkeeping_divergence_does_not_leave_close_final(sf):
    with sf() as session:
        order = session.get(Order, "order-1")
        order.bookkeeping_divergence = True
        _fill(session)
        invalidate_hydra_close_after_late_fill(session, order, RECEIVED)
        attempt = session.get(HydraExecutionAttempt, "attempt-1")
        assert attempt.status == "CLOSED_PENDING_RECONCILIATION"
        assert attempt.risk_snapshot["late_fill_review"]["events"][0]["bookkeeping_divergence"] is True
        assert order.bookkeeping_divergence is True
        assert session.query(Trade).count() == 1


def test_normal_open_attempt_and_non_hydra_fill_are_not_reopened(sf):
    with sf() as session:
        order = session.get(Order, "order-1")
        attempt = session.get(HydraExecutionAttempt, "attempt-1")
        attempt.status = "PENDING"
        _fill(session)
        invalidate_hydra_close_after_late_fill(session, order, RECEIVED)
        assert attempt.status == "PENDING"
        assert "late_fill_review" not in attempt.risk_snapshot
        order.attempt_id = None
        attempt.status = "COMPLETE"
        invalidate_hydra_close_after_late_fill(session, order, RECEIVED)
        assert attempt.status == "COMPLETE"


def test_no_positive_trade_does_not_invalidate_close(sf):
    with sf() as session:
        _fill(session, 0)
        invalidate_hydra_close_after_late_fill(session, session.get(Order, "order-1"), RECEIVED)
        assert session.get(HydraExecutionAttempt, "attempt-1").status == "COMPLETE"


def test_other_account_successor_is_not_modified(sf):
    with sf() as session:
        session.add(_attempt(2, "PENDING", account_alias="another-account"))
        _fill(session)
        invalidate_hydra_close_after_late_fill(session, session.get(Order, "order-1"), RECEIVED)
        assert session.get(HydraExecutionAttempt, "attempt-2").status == "PENDING"
        assert not session.get(HydraExecutionAttempt, "attempt-1").risk_snapshot["late_fill_review"]["requires_manual_resolution"]


def test_helper_does_not_commit_the_callers_transaction(sf):
    with sf() as session:
        _fill(session)
        invalidate_hydra_close_after_late_fill(session, session.get(Order, "order-1"), RECEIVED)
        session.rollback()
    with sf() as session:
        assert session.get(HydraExecutionAttempt, "attempt-1").status == "COMPLETE"
        assert session.query(Trade).count() == 0


@pytest.mark.parametrize("fetched", [False, True])
@pytest.mark.parametrize("new_month", [False, True])
def test_late_fill_holds_newer_same_owner_monthly_or_capital_rebalance(sf, fetched, new_month):
    date = "20261009" if new_month else "20260909"
    created = "2026-10-08T10:00:00+00:00" if new_month else "2026-09-08T10:01:00+00:00"
    with sf() as session:
        _map_owner(session, "order-1", "hydra-owner")
        _other_cycle(session, 3, "hydra-owner", trade_date=date, created_at=created, fetched=fetched)
        session.commit()
    with sf() as session:
        frozen_order = session.get(Order, "order-3")
        before = {column.name: getattr(frozen_order, column.name) for column in Order.__table__.columns}
        _fill(session)
        affected = invalidate_hydra_close_after_late_fill(session, session.get(Order, "order-1"), RECEIVED)
        assert affected == ["attempt-1", "attempt-3"]
        for attempt_id in affected:
            attempt = session.get(HydraExecutionAttempt, attempt_id)
            assert attempt.status == "CLOSED_PENDING_RECONCILIATION"
            review = attempt.risk_snapshot["late_fill_review"]
            assert review["requires_manual_resolution"]
            assert review["affected_successor_rebalance_ids"] == ["rebalance-3"]
            assert review["events"][0]["source_instance_ids"] == ["hydra-owner"]
            assert review["possibly_delivered_successor_order_ids"] == (["order-3"] if fetched else [])
        new_review = session.get(HydraExecutionAttempt, "attempt-3").risk_snapshot["late_fill_review"]
        assert new_review["events"][0]["invalidated_parent"]["target_id"] == "target-3"
        assert new_review["events"][0]["invalidated_parent"]["target_status"] == "STAGED"
        assert session.get(HydraRebalance, "rebalance-3").reconciliation_status == "LATE_FILL_RECONCILIATION_REQUIRED"
        assert session.get(HydraTarget, "target-3").status == "ACTIVE"
        assert {column.name: getattr(frozen_order, column.name) for column in Order.__table__.columns} == before
        session.commit()
    assert OrdersQueueService(sf).list_pending(date, "live", ("live-test",)) == []


def test_cross_rebalance_hold_is_exact_owner_account_domain_and_time_scoped(sf):
    with sf() as session:
        _map_owner(session, "order-1", "hydra-owner")
        _other_cycle(session, 3, "hydra-owner")
        _other_cycle(session, 4, "another-strategy")  # same account and version
        _other_cycle(session, 5, "hydra-owner", trade_date="20260908", created_at="2026-09-07T00:00:00Z")
        _other_cycle(session, 6, "hydra-owner", account="another-account")
        _other_cycle(session, 7, "hydra-owner", domain="paper")
        _other_cycle(session, 8, "hydra-owner", trade_date="20260909", created_at="2026-09-08T09:00:00Z")
        _fill(session)
        affected = invalidate_hydra_close_after_late_fill(session, session.get(Order, "order-1"), RECEIVED)
        assert affected == ["attempt-1", "attempt-3"]
        for number in (4, 5, 6, 7, 8):
            assert session.get(HydraExecutionAttempt, f"attempt-{number}").status == "PENDING"
            assert session.get(HydraTarget, f"target-{number}").status == "STAGED"


def test_cross_rebalance_noop_requires_actual_order_ownership_proof(sf):
    with sf() as session:
        _map_owner(session, "order-1", "hydra-owner")
        _other_cycle(session, 3, "hydra-owner")
        session.add(_attempt(
            8, "NOOP", attempt_number=2, rebalance_id="rebalance-3",
            trade_date="20261009", created_at="2026-10-08T11:00:00Z",
        ))
        _other_cycle(session, 4, None, orderless=True)  # no owner proof; do not guess by version
        _fill(session)
        affected = invalidate_hydra_close_after_late_fill(session, session.get(Order, "order-1"), RECEIVED)
        assert affected == ["attempt-1", "attempt-3", "attempt-8"]
        assert session.get(HydraExecutionAttempt, "attempt-8").status == "CLOSED_PENDING_RECONCILIATION"
        assert session.get(HydraExecutionAttempt, "attempt-4").status == "NOOP"
        assert session.get(HydraTarget, "target-4").status == "STAGED"


def test_unresolved_review_query_prevents_next_cycle_bypass_for_same_owner_only(sf):
    with sf() as session:
        _map_owner(session, "order-1", "hydra-owner")
        _other_cycle(session, 3, "hydra-owner")
        _other_cycle(session, 4, "another-owner")
        _fill(session)
        invalidate_hydra_close_after_late_fill(session, session.get(Order, "order-1"), RECEIVED)
        # Even if all frozen orders are terminal, required old-batch review
        # cannot be bypassed by staging a third rebalance for the same owner.
        session.get(Order, "order-3").status = "FILLED"
        assert unresolved_late_fill_review(session, "hydra-owner", "live", "live-test") == ["attempt-1", "attempt-3"]
        assert unresolved_late_fill_review(session, "another-owner", "live", "live-test") == []
        assert unresolved_late_fill_review(session, "hydra-owner", "paper", "live-test") == []
        assert unresolved_late_fill_review(session, "hydra-owner", "live", "different-account") == []
        for attempt_id in ("attempt-1", "attempt-3"):
            attempt = session.get(HydraExecutionAttempt, attempt_id)
            risk = deepcopy(attempt.risk_snapshot)
            risk["late_fill_review"]["requires_manual_resolution"] = False
            attempt.risk_snapshot = risk
        assert unresolved_late_fill_review(session, "hydra-owner", "live", "live-test") == []


def test_normal_policy_expiry_does_not_create_manual_review_gate(sf):
    with sf() as session:
        _map_owner(session, "order-1", "hydra-owner")
        assert session.get(Order, "order-1").status == "EXPIRED_BY_POLICY"
        assert unresolved_late_fill_review(session, "hydra-owner", "live", "live-test") == []
