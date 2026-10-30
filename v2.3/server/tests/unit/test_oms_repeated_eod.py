"""Repeated QMT day-end states cannot reopen a settled policy-expired order."""
from datetime import datetime, timedelta

import pytest

from app.models import InstanceState
from app.oms.models import OmsBrokerSnapshot
from tests.unit.test_oms_ledger import ALIAS, BUY, CN, DAY, INSTANCE, _broker, _create, _setup, _state


CLOSED = datetime(2026, 10, 9, 15, 5, tzinfo=CN)
REPEATED = CLOSED.replace(minute=30)


def expired_order(tmp_path, *, status=55, traded=400, quantity=1000, project=True, evidence=True):
    sf, ledger = _setup(tmp_path)
    coid = _create(sf, ledger, [BUY])[0]
    broker = _broker(coid, status, traded=traded, price=2.226 if traded else 0., qty=quantity)
    if evidence:
        with sf() as session:
            session.add(OmsBrokerSnapshot(snapshot_id="synthetic-terminal-eod", account_alias=ALIAS,
                kind="EOD", trade_date=DAY, taken_at=CLOSED.isoformat(), received_at=CLOSED.isoformat(),
                payload={"orders": [broker.model_dump(mode="json")]}))
            session.commit()
    assert not ledger.observe_orders(ALIAS, [broker], CLOSED)["conflicts"]
    assert not ledger.finalize_day(ALIAS, DAY, [broker], CLOSED)["anomalies"]
    if project:
        assert ledger.project(ALIAS, DAY)["matched_count"] == 1
    return sf, ledger, coid, broker


def order_facts(order):
    return (order.state, order.filled_qty, order.avg_price, order.projected_qty,
            order.projected_status, order.last_observed_at, order.updated_at)


@pytest.mark.parametrize("status,traded,quantity", [(50, 0, 1000), (55, 400, 1000),
                                                  (52, 400, 1000), (55, 400, 700)])
def test_settled_expiry_repeat_preserves_terminal_state_amount_and_time(tmp_path, status, traded, quantity):
    sf, ledger, coid, broker = expired_order(tmp_path, status=status, traded=traded, quantity=quantity)
    before = order_facts(_state(sf, coid))
    result = ledger.observe_orders(ALIAS, [broker], REPEATED)
    assert result == {"matched": 1, "external": [], "conflicts": []}
    assert order_facts(_state(sf, coid)) == before
    assert ledger.project(ALIAS, DAY)["matched_count"] == 0


@pytest.mark.parametrize("change,reason", [
    ({"quantity": 900}, "ILLEGAL_TRANSITION"),
    ({"traded_volume": 300}, "ILLEGAL_TRANSITION"),
    ({"traded_price": 2.227}, "ILLEGAL_TRANSITION"),
    ({"price": 2.229}, "BROKER_ORDER_IDENTITY_MISMATCH"),
    ({"symbol": "510300.SH"}, "BROKER_ORDER_IDENTITY_MISMATCH"),
    ({"side": "SELL"}, "BROKER_ORDER_IDENTITY_MISMATCH"),
    ({"broker_order_id": "different"}, "DUPLICATE_BROKER_ORDER"),
    ({"status": 255}, "ILLEGAL_TRANSITION"),
    ({"status": 56}, "ILLEGAL_TRANSITION"),
])
def test_changed_broker_facts_are_not_treated_as_repeat(tmp_path, change, reason):
    sf, ledger, coid, broker = expired_order(tmp_path)
    result = ledger.observe_orders(ALIAS, [broker.model_copy(update=change)], REPEATED)
    assert len(result["conflicts"]) == 1
    assert reason in result["conflicts"][0]["reason"]
    order = _state(sf, coid)
    assert order.state == "EXPIRED_DAY" and order.filled_qty == order.projected_qty == 400


def test_additional_fill_is_reported_and_remains_projectable(tmp_path):
    sf, ledger, coid, broker = expired_order(tmp_path)
    changed = broker.model_copy(update={"traded_volume": 500})
    result = ledger.observe_orders(ALIAS, [changed], REPEATED)
    assert result["conflicts"]  # a new economic fact after terminality requires review
    observed = _state(sf, coid)
    assert observed.filled_qty == 500 and observed.projected_qty == 400
    assert ledger.project(ALIAS, DAY)["matched_count"] == 1
    with sf() as session:
        assert session.get(InstanceState, INSTANCE).virtual_positions == {"513100.SH": 500}
    assert _state(sf, coid).projected_qty == 500


@pytest.mark.parametrize("project,evidence,at", [
    (False, True, REPEATED),
    (True, False, REPEATED),
    (True, True, REPEATED + timedelta(days=1)),
    (True, True, CLOSED.replace(hour=14)),
])
def test_missing_settlement_evidence_or_wrong_day_cannot_use_repeat_exception(tmp_path, project, evidence, at):
    _, ledger, _, broker = expired_order(tmp_path, project=project, evidence=evidence)
    assert ledger.observe_orders(ALIAS, [broker], at)["conflicts"]
