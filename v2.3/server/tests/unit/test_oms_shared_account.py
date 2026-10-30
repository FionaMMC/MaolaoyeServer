"""Shared broker account keeps strategy intent, cash and T+1 inventory distinct."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState
from app.oms.cycles import CycleConflict, CycleService
from app.oms.ledger import OrderLedger
from app.oms.manual import ManualOrderIn, ManualService
from app.oms.models import OmsCycle, OmsOrder, OmsSession, OmsTargetVersion
from app.oms.planner import Policy, policy_to_dict
from app.oms.schemas import SnapshotIn, plan_content_sha256
from app.services.reconcile import ReconcileService
from app.services.settlement import SettlementService

ALIAS = "hydra-live"
HYDRA = "live_hydra_v481_rb"
V713 = "live_v713_base"
FINGERPRINT = "a" * 64
CAL = ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023"]
PRICES = {"510300.SH": 4.0, "000001.SZ": 10.0, "000002.SZ": 10.23}
CN = timezone(timedelta(hours=8))


def setup_shared(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/shared.db")
    init_db(engine)
    sf = make_session_factory(engine)
    stock = {"asset_type": "stock", "price_tick": .01, "commission_rate": .0003,
             "stamp_duty_sell": .0005, "t_plus_one": True}
    etf = {"asset_type": "etf", "price_tick": .001, "commission_rate": .0001,
           "stamp_duty_sell": 0., "t_plus_one": True}
    policy = Policy(policy_id="V713_TEST_EXPLICIT", strategy_id="v713", require_instrument_specs=True,
                    buy_bps=(), instruments={"000001.SZ": stock, "000002.SZ": stock, "510300.SH": etf})
    with sf() as session:
        session.add(InstanceState(instance_id=HYDRA, execution_domain="live", account_alias=ALIAS,
            ledger_mode="attributed", virtual_cash=10000., virtual_positions={"510300.SH": 1000},
            owned_symbols=["510300.SH"], last_update="2026-10-16T15:00:00+08:00"))
        session.add(InstanceState(instance_id=V713, execution_domain="live", account_alias=ALIAS,
            ledger_mode="attributed", virtual_cash=20000., virtual_positions={"000001.SZ": 1000},
            owned_symbols=["000001.SZ"], strategy_state={"oms_enabled": True},
            last_update="2026-10-16T15:00:00+08:00"))
        session.commit()
    service = CycleService(sf, OrderLedger(sf, SettlementService(sf, commission_rate=.0001)), ReconcileService(sf),
                           account_fingerprint=FINGERPRINT)
    common = dict(account_alias=ALIAS, signal_date="20261016", signal_closes=PRICES, calendar=CAL,
                  now="2026-10-18T20:00:00+08:00")
    hydra = service.publish_target(instance_id=HYDRA, weights={"510300.SH": 1.}, source_sha256="h" * 64, **common)
    v713 = service.publish_target(instance_id=V713, weights={"000002.SZ": .5, "510300.SH": .5},
                                   source_sha256="v" * 64, policy=policy, **common)
    for row in (hydra, v713):
        service.approve(row["cycle_id"], "synthetic-test", "2026-10-18T21:00:00+08:00")
    return sf, service, policy, hydra, v713


def snapshot(day="20261019", hhmm="1445", kind="PRE", **changes):
    payload = dict(account_alias=ALIAS, trade_date=day, kind=kind,
        taken_at=f"{day[:4]}-{day[4:6]}-{day[6:]}T{hhmm[:2]}:{hhmm[2:]}:00+08:00",
        available_cash=30000., total_asset=44000., positions={"510300.SH": 1000, "000001.SZ": 1000},
        sellable={"510300.SH": 1000, "000001.SZ": 1000}, orders=[], trades=[],
        quotes={symbol: {"last_price": price, "is_trading": False,
                         "adjustment": "none", "price_source": "raw_tick",
                         "source_time": f"{day[:4]}-{day[4:6]}-{day[6:]}T15:00:00+08:00"}
                for symbol, price in PRICES.items()})
    payload.update(changes)
    return SnapshotIn(**payload)


def test_two_cycles_publish_and_old_stock_gets_explicit_exit(tmp_path):
    sf, service, _, hydra, v713 = setup_shared(tmp_path)
    assert hydra["cycle_id"] != v713["cycle_id"]
    with sf() as session:
        cycle = session.get(OmsCycle, v713["cycle_id"])
        assert cycle.frozen_target["000001.SZ"] == 0
        exits = session.query(OmsOrder).filter_by(cycle_id=cycle.cycle_id, symbol="000001.SZ", side="SELL").all()
        assert len(exits) == 1 and exits[0].quantity == 1000
    with pytest.raises(ValueError, match="multiple strategy"):
        service.plan_for(ALIAS, "20261019", "SELL", executable_flag=True)
    assert not service.plan_for(ALIAS, "20261019", "SELL", instance_id=V713, executable_flag=True).executable


def test_false_closing_state_advances_both_cycles_and_policy_is_frozen(tmp_path):
    sf, service, policy, _, v713 = setup_shared(tmp_path)
    service.policy = Policy(default_buy_bps=900.)
    result = service.ingest_snapshot(snapshot(hhmm="1505", kind="EOD"), "2026-10-19T15:05:00+08:00")
    assert result["reconciliation"]["passed"]
    assert result["cycle_statuses"] == {HYDRA: "ACTIVE", V713: "ACTIVE"}
    buy = service.plan_for(ALIAS, "20261020", "BUY", instance_id=V713, executable_flag=True)
    assert buy.orders and all(item.limit_price != 0 for item in buy.orders)
    stock_order = next(item for item in buy.orders if item.symbol == "000002.SZ")
    assert stock_order.limit_price == 10.28  # frozen 50bp rule with a stock tick, not runtime 900bp
    with sf() as session:
        assert session.get(OmsCycle, v713["cycle_id"]).policy["policy_id"] == policy.policy_id


def test_v2_grants_own_inventory_hash_and_short_lease(tmp_path):
    sf, service, _, _, _ = setup_shared(tmp_path)
    service.ingest_snapshot(snapshot(), "2026-10-19T14:45:00+08:00")
    bundle = service.plans_for(ALIAS, "20261019", "SELL", executable_flag=True,
                               now="2026-10-19T14:46:00+08:00")
    assert len(bundle["plans"]) == 2
    plans = {row["instance_id"]: row for row in bundle["plans"]}
    assert plans[V713]["strategy_positions"] == {"000001.SZ": 1000}
    assert plans[HYDRA]["strategy_positions"] == {"510300.SH": 1000}
    for row in bundle["plans"]:
        assert row["executable"] and row["account_fingerprint"] == FINGERPRINT
        assert row["plan_sha256"] == plan_content_sha256(row)
        assert row["lease_expires_at"] == "2026-10-19T15:00:00+08:00"
    old = service.plans_for(ALIAS, "20261019", "SELL", executable_flag=True,
                            now="2026-10-19T15:01:00+08:00")
    assert not any(row["executable"] for row in old["plans"])


def test_idempotent_publish_and_policy_change_refused(tmp_path):
    sf, service, policy, _, v713 = setup_shared(tmp_path)
    args = dict(instance_id=V713, account_alias=ALIAS, signal_date="20261016",
        weights={"000002.SZ": .5, "510300.SH": .5}, signal_closes=PRICES, calendar=CAL,
        source_sha256="v" * 64, now="2026-10-18T20:00:00+08:00", policy=policy)
    assert service.publish_target(**args)["cycle_id"] == v713["cycle_id"]
    with sf() as session:
        assert session.query(OmsTargetVersion).count() == 2
    args["policy"] = {**policy_to_dict(policy), "default_buy_bps": 100.}
    with pytest.raises(CycleConflict, match="different prices or policy"):
        service.publish_target(**args)


def test_manual_orders_require_strategy_and_global_flag_preserves_cancels(tmp_path):
    sf, service, _, _, _ = setup_shared(tmp_path)
    manual = ManualService(sf, service)
    now = datetime(2026, 10, 19, 10, tzinfo=CN)
    with pytest.raises(ValueError, match="select exactly one"):
        manual.create_order(ManualOrderIn(account_alias=ALIAS, symbol="510300.SH", side="BUY", quantity=100,
                                          limit_price=4., reason="test intent", operator="test", confirm=True), now)
    manual.control(ALIAS, V713, False, "test", "pause own strategy", now)
    with pytest.raises(ValueError, match="disabled"):
        manual.create_order(ManualOrderIn(account_alias=ALIAS, instance_id=V713, symbol="000002.SZ", side="BUY",
                          quantity=100, limit_price=10.23, reason="test intent", operator="test", confirm=True), now)
    universe = service.universe(ALIAS)
    assert {row["instance_id"]: row["enabled"] for row in universe["instances"]} == {HYDRA: True, V713: False}


def test_stale_quote_does_not_create_v713_buy_anchor(tmp_path):
    sf, service, _, _, v713 = setup_shared(tmp_path)
    snap = snapshot(hhmm="1505", kind="EOD")
    snap.quotes["000002.SZ"].source_time = "2026-10-16T15:00:00+08:00"
    service.ingest_snapshot(snap, "2026-10-19T15:05:00+08:00")
    with sf() as session:
        cycle = session.get(OmsCycle, v713["cycle_id"])
        assert cycle.status == "ACTIVE" and set(cycle.buy_anchor) == {"510300.SH"}
        assert all(o.symbol != "000002.SZ" for o in session.scalars(select(OmsOrder).where(
            OmsOrder.cycle_id == cycle.cycle_id, OmsOrder.side == "BUY")))


def test_stock_without_a_close_on_the_sell_day_waits_for_its_own_anchor(tmp_path):
    """Bug: one target stock without a usable close on day S held the whole V713 cycle for good."""
    sf, service, _, _, v713 = setup_shared(tmp_path)
    snap = snapshot(hhmm="1505", kind="EOD")
    del snap.quotes["000002.SZ"]                      # suspended on S: no tick and no daily bar
    service.ingest_snapshot(snap, "2026-10-19T15:05:00+08:00")
    with sf() as session:
        cycle = session.get(OmsCycle, v713["cycle_id"])
        assert cycle.status == "ACTIVE" and set(cycle.buy_anchor) == {"510300.SH"}
        buy = session.scalars(select(OmsSession).where(OmsSession.cycle_id == cycle.cycle_id,
                                                       OmsSession.trade_date == "20261020",
                                                       OmsSession.phase == "BUY")).one()
        assert [(d["symbol"], d["reason"]) for d in buy.deferrals if d["symbol"] == "000002.SZ"] == [
            ("000002.SZ", "NO_ANCHOR")]
    service.ingest_snapshot(snapshot(day="20261020", hhmm="1505", kind="EOD"), "2026-10-20T15:05:00+08:00")
    with sf() as session:
        cycle = session.get(OmsCycle, v713["cycle_id"])
        assert cycle.status == "ACTIVE" and cycle.buy_anchor["000002.SZ"] == 10.23   # its first usable close
        later = session.scalars(select(OmsOrder).where(OmsOrder.cycle_id == cycle.cycle_id,
                                                       OmsOrder.side == "BUY", OmsOrder.symbol == "000002.SZ")).all()
        assert later and {o.trade_date for o in later} == {"20261021"}


def test_no_usable_close_for_any_target_still_holds(tmp_path):
    sf, service, _, hydra, v713 = setup_shared(tmp_path)
    snap = snapshot(hhmm="1505", kind="EOD")
    for symbol in ("000002.SZ", "510300.SH"):
        snap.quotes[symbol].source_time = "2026-10-19T09:30:00+08:00"
    service.ingest_snapshot(snap, "2026-10-19T15:05:00+08:00")
    with sf() as session:
        cycle = session.get(OmsCycle, v713["cycle_id"])
        assert cycle.status == "HELD" and cycle.buy_anchor is None
        assert session.get(OmsCycle, hydra["cycle_id"]).status == "ACTIVE"   # Hydra's own rules are unaffected

