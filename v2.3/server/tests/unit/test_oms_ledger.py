from datetime import datetime, timedelta, timezone

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState, Order, OrderSignalMap, RawSignal
from app.oms.ledger import OrderLedger, client_order_id
from app.oms.models import OmsCycle, OmsFill, OmsOrder, OmsOrderEvent, OmsSession
from app.oms.planner import PlannedOrder
from app.oms.schemas import BrokerOrder, BrokerTrade, EventIn
from app.services.orders_queue import OrdersQueueService
from app.services.settlement import SettlementService

CN = timezone(timedelta(hours=8))
ALIAS = "hydra-live"
INSTANCE = "live_hydra_v481_rb"
DAY = "20261009"
NOW = "2026-10-09T09:00:00+08:00"


def _setup(tmp_path, cash=200000.0, positions=None):
    engine = make_engine(f"sqlite:///{tmp_path}/ledger.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(InstanceState(instance_id=INSTANCE, execution_domain="live", account_alias=ALIAS,
                            ledger_mode="attributed", virtual_cash=cash, virtual_positions=positions or {},
                            owned_symbols=["513100.SH", "510300.SH"], last_update=NOW))
        s.add(OmsCycle(cycle_id="C00001", cycle_no=1, target_version_id="tv1", account_alias=ALIAS,
                       instance_id=INSTANCE, policy={}, nav_at_signal=cash, frozen_target={}, sell_anchor={},
                       lot_gap=0.0, schedule=[], status="ACTIVE", created_at=NOW))
        s.add(OmsSession(session_id="C00001:2", cycle_id="C00001", seq=2, trade_date=DAY, phase="BUY",
                         status="PLANNED", plan_sha256="p" * 64, deferrals=[], created_at=NOW))
        s.commit()
    settlement = SettlementService(sf, commission_rate=0.0001, min_commission=5.0, stamp_duty_sell=0.0)
    return sf, OrderLedger(sf, settlement)


def _create(sf, ledger, planned):
    with sf() as s:
        cycle, sess = s.get(OmsCycle, "C00001"), s.get(OmsSession, "C00001:2")
        rows = ledger.create_orders(s, cycle=cycle, oms_session=sess, planned=planned, now=NOW)
        s.commit()
        return [r.client_order_id for r in rows]


BUY = PlannedOrder("513100.SH", "BUY", 1000, 2.228, 2.196)
BUY2 = PlannedOrder("510300.SH", "BUY", 500, 4.644, 4.621)


def _event(kind, coid="H000010201", eid="e" * 31, broker=None):
    return EventIn(event_id=eid + kind[0], client_order_id=coid, kind=kind,
                   observed_at=datetime(2026, 10, 9, 9, 15, 6, tzinfo=CN), broker_order_id=broker)


def _broker(remark, status, traded=0, price=0.0, qty=1000, broker_id="900001", symbol="513100.SH"):
    return BrokerOrder(broker_order_id=broker_id, symbol=symbol, side="BUY", quantity=qty, price=2.228,
                       traded_volume=traded, traded_price=price, status=status, remark=remark)


def _state(sf, coid):
    with sf() as s:
        return s.get(OmsOrder, coid)


def test_client_order_id_is_short_and_stable():
    assert client_order_id(1, 2, 1) == "H000010201"
    assert len(client_order_id(99999, 99, 99)) == 10


def test_create_orders_writes_mirror_rows_not_visible_to_legacy_orders_api(tmp_path):
    sf, ledger = _setup(tmp_path)
    ids = _create(sf, ledger, [BUY, BUY2])
    assert ids == ["H000010201", "H000010202"]
    with sf() as s:
        order = s.get(Order, "H000010201")
        assert order.status == "OMS_ROUTED" and order.execution_domain == "live"
        assert order.target_id == "tv1" and order.attempt_id is None and order.limit_price == 2.228
        mapping = s.get(OrderSignalMap, ("H000010201", s.query(RawSignal).filter_by(symbol="513100.SH").one().signal_id))
        assert mapping.signal_quantity == 1000
    assert OrdersQueueService(sf).list_pending(DAY, "live", (ALIAS,)) == []
    # A second planning pass for the same session continues the leg numbering.
    assert _create(sf, ledger, [BUY2]) == ["H000010203"]


def test_events_are_idempotent_and_illegal_transition_is_recorded_not_raised(tmp_path):
    sf, ledger = _setup(tmp_path)
    _create(sf, ledger, [BUY])
    out = ledger.apply_events(ALIAS, [_event("SUBMIT_STARTED"), _event("ACKED", broker="900001")], NOW)
    assert out["applied"] == 2 and _state(sf, "H000010201").state == "ACKED"
    assert _state(sf, "H000010201").broker_order_id == "900001"
    again = ledger.apply_events(ALIAS, [_event("ACKED", broker="900001")], NOW)
    assert again["duplicate"] == 1 and again["applied"] == 0
    illegal = ledger.apply_events(ALIAS, [_event("SUBMIT_STARTED", eid="f" * 31)], NOW)
    assert illegal["applied"] == 0 and illegal["rejected"]
    with sf() as s:
        row = s.get(OmsOrderEvent, "f" * 31 + "S")
        assert row.applied is False and "ACKED -> SUBMITTING" in row.error
    foreign = ledger.apply_events("other-account", [_event("ACKED", eid="g" * 31)], NOW)
    assert foreign["rejected"]


def test_observe_orders_maps_status_and_never_decreases_filled(tmp_path):
    sf, ledger = _setup(tmp_path)
    _create(sf, ledger, [BUY])
    at = datetime(2026, 10, 9, 10, 0, tzinfo=CN)
    ledger.observe_orders(ALIAS, [_broker("H000010201", 55, traded=400, price=2.226)], at)
    o = _state(sf, "H000010201")
    assert o.state == "PARTIAL" and o.filled_qty == 400 and o.avg_price == 2.226
    ledger.observe_orders(ALIAS, [_broker("H000010201", 55, traded=300, price=2.2)], at)
    assert _state(sf, "H000010201").filled_qty == 400
    ledger.observe_orders(ALIAS, [_broker("H000010201", 56, traded=1000, price=2.227)], at)
    assert _state(sf, "H000010201").state == "FILLED"


def test_second_broker_order_with_same_remark_is_flagged_as_duplicate(tmp_path):
    sf, ledger = _setup(tmp_path)
    _create(sf, ledger, [BUY])
    at = datetime(2026, 10, 9, 10, 0, tzinfo=CN)
    ledger.observe_orders(ALIAS, [_broker("H000010201", 50, broker_id="900001")], at)
    out = ledger.observe_orders(ALIAS, [_broker("H000010201", 50, broker_id="900001"),
                                        _broker("H000010201", 50, broker_id="900777")], at)
    assert out["conflicts"] == [{"client_order_id": "H000010201", "reason": "DUPLICATE_BROKER_ORDER",
                                 "broker_order_id": "900777"}]
    assert _state(sf, "H000010201").broker_order_id == "900001"


def test_non_oms_broker_orders_are_reported_as_external(tmp_path):
    sf, ledger = _setup(tmp_path)
    out = ledger.observe_orders(ALIAS, [_broker("manual", 50, broker_id="1")], datetime(2026, 10, 9, 10, tzinfo=CN))
    assert out["external"][0]["broker_order_id"] == "1" and out["external"][0]["open"] is True


def test_finalize_day_expires_status_50_after_close_and_marks_missing_as_not_submitted(tmp_path):
    sf, ledger = _setup(tmp_path)
    _create(sf, ledger, [BUY, BUY2])
    ledger.apply_events(ALIAS, [_event("SUBMIT_STARTED"), _event("ACKED", broker="900001"),
                                _event("SUBMIT_STARTED", coid="H000010202", eid="h" * 31)], NOW)
    with pytest.raises(ValueError):
        ledger.finalize_day(ALIAS, DAY, [], datetime(2026, 10, 9, 14, 59, tzinfo=CN))
    done = ledger.finalize_day(ALIAS, DAY, [_broker("H000010201", 50)], datetime(2026, 10, 9, 15, 5, tzinfo=CN))
    assert sorted(done["finalized"]) == ["H000010201", "H000010202"]
    assert _state(sf, "H000010201").state == "EXPIRED_DAY"
    assert _state(sf, "H000010202").state == "NOT_SUBMITTED"


def test_project_updates_instance_state_exactly_once(tmp_path):
    sf, ledger = _setup(tmp_path)
    _create(sf, ledger, [BUY])
    ledger.apply_events(ALIAS, [_event("SUBMIT_STARTED"), _event("ACKED", broker="900001")], NOW)
    ledger.observe_orders(ALIAS, [_broker("H000010201", 56, traded=1000, price=2.225)],
                          datetime(2026, 10, 9, 9, 31, tzinfo=CN))
    first = ledger.project(ALIAS, DAY)
    assert first["matched_count"] == 1
    with sf() as s:
        inst = s.get(InstanceState, INSTANCE)
        assert inst.virtual_positions == {"513100.SH": 1000}
        assert inst.virtual_cash == pytest.approx(200000 - 2225 - 5)
    assert ledger.project(ALIAS, DAY)["matched_count"] == 0
    with sf() as s:
        assert s.get(InstanceState, INSTANCE).virtual_positions == {"513100.SH": 1000}


def test_partial_then_expired_projects_cancelled_with_filled_quantity(tmp_path):
    sf, ledger = _setup(tmp_path)
    _create(sf, ledger, [BUY])
    ledger.apply_events(ALIAS, [_event("SUBMIT_STARTED"), _event("ACKED", broker="900001")], NOW)
    at = datetime(2026, 10, 9, 10, 0, tzinfo=CN)
    ledger.observe_orders(ALIAS, [_broker("H000010201", 55, traded=400, price=2.226)], at)
    ledger.project(ALIAS, DAY)
    ledger.finalize_day(ALIAS, DAY, [_broker("H000010201", 55, traded=400, price=2.226)],
                        datetime(2026, 10, 9, 15, 5, tzinfo=CN))
    ledger.project(ALIAS, DAY)
    with sf() as s:
        assert s.get(Order, "H000010201").status == "CANCELLED"
        assert s.get(InstanceState, INSTANCE).virtual_positions == {"513100.SH": 400}
    assert _state(sf, "H000010201").projected_status == "CANCELLED"


def test_trades_dedupe_on_broker_trade_id_and_flag_external(tmp_path):
    sf, ledger = _setup(tmp_path)
    _create(sf, ledger, [BUY])
    trades = [BrokerTrade(broker_trade_id="T1", broker_order_id="900001", symbol="513100.SH", side="BUY",
                          quantity=400, price=2.226, traded_at="09:30:01", remark="H000010201"),
              BrokerTrade(broker_trade_id="T2", broker_order_id="5", symbol="510300.SH", side="BUY",
                          quantity=100, price=4.6, traded_at="10:00:00", remark="manual")]
    assert ledger.record_trades(ALIAS, trades, NOW) == 2
    assert ledger.record_trades(ALIAS, trades, NOW) == 0
    with sf() as s:
        by_id = {f.broker_trade_id: f for f in s.query(OmsFill).all()}
        assert by_id["T1"].client_order_id == "H000010201" and by_id["T2"].client_order_id is None
