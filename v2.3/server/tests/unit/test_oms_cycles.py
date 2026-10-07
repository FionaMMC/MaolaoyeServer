from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState
from app.oms.cycles import CycleConflict, CycleService
from app.oms.ledger import OrderLedger
from app.oms.models import OmsCycle, OmsOrder, OmsSession
from app.oms.schemas import SnapshotIn
from app.services.reconcile import ReconcileService
from app.services.settlement import SettlementService

CN = timezone(timedelta(hours=8))
ALIAS = "hydra-live"
INSTANCE = "live_hydra_v481_rb"
CAL = ["20260929", "20260930", "20261008", "20261009", "20261012", "20261013", "20261014", "20261015"]
WEIGHTS = {"510300.SH": 0.05, "513100.SH": 0.95}
CLOSES = {"510300.SH": 4.60, "513100.SH": 2.20}


def _service(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/cycles.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(InstanceState(instance_id=INSTANCE, execution_domain="live", account_alias=ALIAS,
                            ledger_mode="attributed", virtual_cash=100000.0, virtual_positions={"510300.SH": 3000},
                            owned_symbols=sorted(WEIGHTS), last_update="x"))
        s.commit()
    settlement = SettlementService(sf, commission_rate=0.0001, min_commission=5.0, stamp_duty_sell=0.0)
    return sf, CycleService(sf, OrderLedger(sf, settlement), ReconcileService(sf))


def _publish(svc):
    return svc.publish_target(instance_id=INSTANCE, account_alias=ALIAS, signal_date="20260930", weights=WEIGHTS,
                              signal_closes=CLOSES, calendar=CAL, source_sha256="s" * 64,
                              now="2026-10-07T20:00:00+08:00")


def _ledger(sf):
    with sf() as s:
        inst = s.get(InstanceState, INSTANCE)
        return dict(inst.virtual_positions), float(inst.virtual_cash)


def _orders(sf, session_id):
    with sf() as s:
        return [o for o in s.query(OmsOrder).filter_by(session_id=session_id).order_by(OmsOrder.client_order_id)]


def _snap(day, hhmm, kind, positions, cash, orders=(), quotes=None):
    return SnapshotIn(account_alias=ALIAS, kind=kind, trade_date=day,
                      taken_at=datetime(int(day[:4]), int(day[4:6]), int(day[6:]), int(hhmm[:2]), int(hhmm[2:]),
                                        tzinfo=CN),
                      available_cash=cash, total_asset=cash, positions=positions, sellable=positions,
                      orders=list(orders), trades=None,
                      quotes=quotes or {s: {"last_price": p} for s, p in CLOSES.items()})


def _fill(order, traded, price, status=56):
    return dict(broker_order_id=f"9{order.client_order_id[1:]}", symbol=order.symbol, side=order.side,
                quantity=order.quantity, price=order.limit_price, traded_volume=traded, traded_price=price,
                status=status, remark=order.client_order_id)


def test_publish_creates_pending_cycle_with_first_sell_plan_not_executable(tmp_path):
    sf, svc = _service(tmp_path)
    out = _publish(svc)
    assert out["status"] == "PENDING_APPROVAL" and out["cycle_id"] == "C00001"
    assert [x["trade_date"] for x in out["schedule"]] == ["20261008", "20261009", "20261012", "20261013"]
    plan = svc.plan_for(ALIAS, "20261008", "SELL", executable_flag=True)
    assert plan.executable is False and plan.session_id == "C00001:1"
    assert [(o.symbol, o.side) for o in plan.orders] == [("510300.SH", "SELL")]
    assert plan.orders[0].limit_price == 4.577          # 4.60 * (1 - 50bp) rounded up to the tick
    shares = {row["symbol"]: row for row in out["shares"]}
    assert shares["510300.SH"]["held"] == 3000 and shares["510300.SH"]["delta"] < 0


def test_full_october_window_plans_from_broker_facts_and_closes_with_report(tmp_path):
    sf, svc = _service(tmp_path)
    _publish(svc)
    svc.approve("C00001", "tester", "2026-10-07T21:00:00+08:00")
    assert svc.plan_for(ALIAS, "20261008", "SELL", executable_flag=True).executable is True

    pre = svc.ingest_snapshot(_snap("20261008", "1445", "PRE", {"510300.SH": 3000}, 100000.0), "n")
    assert pre["reconciliation"]["passed"] is True

    sell = _orders(sf, "C00001:1")[0]
    svc.ledger.apply_events(ALIAS, [], "n")
    proceeds = sell.quantity * 4.58 - max(5.0, sell.quantity * 4.58 * 0.0001)
    remaining = 3000 - sell.quantity
    eod = svc.ingest_snapshot(_snap("20261008", "1505", "EOD", {"510300.SH": remaining}, 100000.0 + proceeds,
                                    orders=[_fill(sell, sell.quantity, 4.58)]), "n")
    assert eod["reconciliation"]["passed"] is True
    positions, cash = _ledger(sf)
    assert positions == {"510300.SH": remaining} and cash == pytest.approx(100000.0 + proceeds)
    with sf() as s:
        cycle = s.get(OmsCycle, "C00001")
        assert cycle.buy_anchor == CLOSES
    buy_plan = svc.plan_for(ALIAS, "20261009", "BUY", executable_flag=True)
    buy = buy_plan.orders[0]
    assert buy.symbol == "513100.SH" and buy.limit_price == 2.233      # 2.20 * (1 + 150bp), down to the tick
    assert buy.quantity * buy.limit_price <= cash

    # 10/9: only part of the buy fills, the rest expires at 15:00 (status stays 55).
    buy_row = _orders(sf, "C00001:2")[0]
    part = buy_row.quantity // 2 // 100 * 100
    cost = part * 2.21 + max(5.0, part * 2.21 * 0.0001)
    svc.ingest_snapshot(_snap("20261009", "1505", "EOD", {"510300.SH": remaining, "513100.SH": part},
                              100000.0 + proceeds - cost, orders=[_fill(buy_row, part, 2.21, status=55)]), "n")
    assert _orders(sf, "C00001:2")[0].state == "EXPIRED_DAY"
    # 10/12 sell attempt has nothing left to sell; the plan exists but is empty.
    assert svc.plan_for(ALIAS, "20261012", "SELL", executable_flag=True).orders == []
    svc.ingest_snapshot(_snap("20261012", "1505", "EOD", {"510300.SH": remaining, "513100.SH": part},
                              100000.0 + proceeds - cost), "n")
    top_up = svc.plan_for(ALIAS, "20261013", "BUY", executable_flag=True).orders[0]
    with sf() as s:
        frozen = s.get(OmsCycle, "C00001").frozen_target
    assert frozen == {"510300.SH": 1200, "513100.SH": 49100}
    # Residual is capped by the frozen target, never by fresh valuation.
    assert top_up.symbol == "513100.SH" and top_up.quantity <= frozen["513100.SH"] - part
    assert top_up.limit_price == 2.233                                   # anchor fixed for the whole cycle
    final = svc.ingest_snapshot(_snap("20261013", "1505", "EOD", {"510300.SH": remaining, "513100.SH": part},
                                      100000.0 + proceeds - cost), "n")
    assert final["cycle_status"] == "CLOSED"
    report = svc.report("C00001")
    assert report["close_reason"] == "WINDOW_COMPLETE"
    assert report["exec_underweight"] > 0 and "lot_gap" in report
    assert report["unfinished"]["513100.SH"]["reason"] == "NOT_SUBMITTED"


def test_position_mismatch_holds_cycle_and_blocks_planning(tmp_path):
    sf, svc = _service(tmp_path)
    _publish(svc)
    svc.approve("C00001", "tester", "n")
    out = svc.ingest_snapshot(_snap("20261008", "1505", "EOD", {"510300.SH": 2900}, 100000.0), "n")
    assert out["reconciliation"]["passed"] is False and out["cycle_status"] == "HELD"
    assert svc.plan_for(ALIAS, "20261008", "SELL", executable_flag=True).executable is False
    with sf() as s:
        assert s.query(OmsSession).filter_by(trade_date="20261009").count() == 0


def test_unknown_open_order_in_whitelisted_symbol_holds(tmp_path):
    sf, svc = _service(tmp_path)
    _publish(svc)
    svc.approve("C00001", "tester", "n")
    manual = dict(broker_order_id="1", symbol="513100.SH", side="BUY", quantity=100, price=2.2, traded_volume=0,
                  traded_price=0.0, status=50, remark="manual")
    out = svc.ingest_snapshot(_snap("20261008", "1445", "PRE", {"510300.SH": 3000}, 100000.0, orders=[manual]), "n")
    assert out["reconciliation"]["passed"] is False
    assert out["reconciliation"]["discrepancies"][0]["type"] == "UNKNOWN_OPEN_BROKER_ORDER"


def test_second_publish_while_cycle_open_is_refused(tmp_path):
    _, svc = _service(tmp_path)
    _publish(svc)
    with pytest.raises(CycleConflict):
        svc.publish_target(instance_id=INSTANCE, account_alias=ALIAS, signal_date="20261030", weights=WEIGHTS,
                           signal_closes=CLOSES, calendar=CAL + ["20261030", "20261102", "20261103", "20261104"],
                           source_sha256="t" * 64, now="2026-10-30T20:00:00+08:00")


def test_unlisted_zero_weight_symbol_needs_no_close_or_anchor(tmp_path):
    sf, svc = _service(tmp_path)
    weights = dict(WEIGHTS, **{"159981.SZ": 0.0})
    closes = dict(CLOSES, **{"159981.SZ": float("nan")})
    svc.publish_target(instance_id=INSTANCE, account_alias=ALIAS, signal_date="20260930", weights=weights,
                       signal_closes=closes, calendar=CAL, source_sha256="u" * 64, now="2026-10-07T20:00:00+08:00")
    svc.approve("C00001", "tester", "n")
    sell = _orders(sf, "C00001:1")[0]
    proceeds = sell.quantity * 4.58 - 5.0
    out = svc.ingest_snapshot(_snap("20261008", "1505", "EOD", {"510300.SH": 3000 - sell.quantity},
                                    100000.0 + proceeds, orders=[_fill(sell, sell.quantity, 4.58)]), "n")
    assert out["cycle_status"] == "ACTIVE"
    with sf() as s:
        assert set(s.get(OmsCycle, "C00001").buy_anchor) == {"510300.SH", "513100.SH"}


def test_positive_weight_without_close_is_refused(tmp_path):
    _, svc = _service(tmp_path)
    with pytest.raises(ValueError):
        svc.publish_target(instance_id=INSTANCE, account_alias=ALIAS, signal_date="20260930",
                           weights=dict(WEIGHTS, **{"159981.SZ": 0.1}), signal_closes=CLOSES, calendar=CAL,
                           source_sha256="v" * 64, now="2026-10-07T20:00:00+08:00")



@pytest.mark.parametrize("now,first_sell", [("2026-10-08T10:00:00+08:00", "20261008"),
                                             ("2026-10-08T14:50:00+08:00", "20261012"),
                                             ("2026-10-09T09:00:00+08:00", "20261012")])
def test_late_publish_starts_at_the_next_pair_still_ahead(tmp_path, now, first_sell):
    """It used to refuse ("schedule already started"), so the runbook's 10/12-10/13 fallback had no path."""
    sf, svc = _service(tmp_path)
    svc.publish_target(instance_id=INSTANCE, account_alias=ALIAS, signal_date="20260930", weights=WEIGHTS,
                       signal_closes=CLOSES, calendar=CAL, source_sha256="l" * 64, now=now)
    with sf() as s:
        cycle = s.get(OmsCycle, "C00001")
        assert cycle.schedule[0]["trade_date"] == first_sell and cycle.schedule[0]["phase"] == "SELL"
        first = s.execute(select(OmsSession).where(OmsSession.cycle_id == "C00001")
                          .order_by(OmsSession.seq)).scalars().first()
        assert first.trade_date == first_sell

# ── regressions for bugs found by the system replays ──────────────────────
def test_open_buy_orders_freeze_commission_per_order_not_once(tmp_path):
    """Bug 1/7: reconciliation added back one minimum commission for all open buys.

    Four small open buys freeze 4 x (200 + 5); the old add-back (800 x 1.001 + 5) fell
    14 CNY short of that and held the cycle on every intraday PRE snapshot."""
    _, svc = _service(tmp_path)
    open_buys = [dict(broker_order_id=str(i), symbol="513100.SH", side="BUY", quantity=100, price=2.0,
                      traded_volume=0, traded_price=0.0, status=50, remark=f"H0000102{i:02d}") for i in range(1, 5)]
    frozen = 4 * (100 * 2.0 + 5.0)
    snap = _snap("20261008", "1445", "PRE", {"510300.SH": 3000}, 100000.0 - frozen, orders=open_buys)
    found = svc._discrepancies(snap, {"external": [], "conflicts": []}, None, {})
    assert [d for d in found if d["type"] == "CASH_SHORTFALL"] == []


def test_eod_before_first_sell_day_does_not_hold(tmp_path):
    """Bug 4: a Friday EOD before a Monday first sell held the cycle (no buy anchor yet)."""
    sf, svc = _service(tmp_path)
    cal = ["20261001", "20261002", "20261005", "20261006", "20261007", "20261008"]   # Thu, Fri, Mon...
    svc.publish_target(instance_id=INSTANCE, account_alias=ALIAS, signal_date="20261001", weights=WEIGHTS,
                       signal_closes=CLOSES, calendar=cal, source_sha256="f" * 64, now="2026-10-01T20:00:00+08:00")
    svc.approve("C00001", "tester", "n")
    out = svc.ingest_snapshot(_snap("20261002", "1505", "EOD", {"510300.SH": 3000}, 100000.0), "n")
    assert out["cycle_status"] == "ACTIVE" and out["reconciliation"]["passed"] is True


def test_cycle_closes_even_if_report_fails(tmp_path, monkeypatch):
    """Bug 3: a report error (unpriced symbol) kept a finished cycle open forever."""
    sf, svc = _service(tmp_path)
    _publish(svc)
    svc.approve("C00001", "tester", "n")
    monkeypatch.setattr(svc, "_build_report", lambda *a, **k: (_ for _ in ()).throw(KeyError("159981.SZ")))
    with sf() as s:
        cycle = s.get(OmsCycle, "C00001")
        svc._close(s, cycle, "WINDOW_COMPLETE", _snap("20261013", "1505", "EOD", {}, 0.0), "n")
        s.commit()
    with sf() as s:
        cycle = s.get(OmsCycle, "C00001")
        assert cycle.status == "CLOSED" and cycle.report["error"] == "REPORT_FAILED"


def test_report_values_unpriced_symbols_without_error(tmp_path):
    """Bug 3: report pricing must tolerate symbols with no close (not yet listed)."""
    sf, svc = _service(tmp_path)
    svc.publish_target(instance_id=INSTANCE, account_alias=ALIAS, signal_date="20260930",
                       weights=dict(WEIGHTS, **{"159981.SZ": 0.0}), signal_closes=dict(CLOSES, **{"159981.SZ": None}),
                       calendar=CAL, source_sha256="r" * 64, now="2026-10-07T20:00:00+08:00")
    report = svc.report("C00001")
    assert "159981.SZ" not in report["unfinished"] and report["nav"] > 0
