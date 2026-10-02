"""End-to-end: real server + real agent + simulated QMT must reproduce the research replay.

The research replay (reports/hydra_policy_20261002/policy_replay.py, policy C3) and the
production system share the planner, so with identical market data and fees the system's
fills, positions and cash must equal the replay's exactly. Fault-injection cases assert
that outages, crashes and unknown submits never create a second broker order and never
let the ledger drift from the broker.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import math
import sys
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.db import make_session_factory
from app.dependencies import _engine_for_url, get_oms_cycle_service, get_settings
from app.main import create_app
from app.models import InstanceState
from app.oms.models import OmsCycle, OmsReconciliation
from app.oms.planner import Policy
from app.settings import Settings
from live_client.oms_agent import OmsAgent
from live_client.oms_journal import OmsJournal
from live_client.sim_exchange import SimExchange, SimQMTGateway

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "reports" / "hydra_policy_20261002"))
policy_replay = pytest.importorskip("policy_replay")

CN = timezone(timedelta(hours=8))
ALIAS, INSTANCE = "hydra-live", "live_hydra_v481_rb"
LIVE = {"Authorization": "Bearer LIVE_KEY"}
SYMBOLS = ["510300.SH", "511260.SH", "513100.SH", "518880.SH"]
BASE = {"510300.SH": 4.6, "511260.SH": 136.0, "513100.SH": 2.2, "518880.SH": 9.1}
CAPITAL = 200000.0


def _r3(x):
    return round(x, 3)


def _bar(open_, high, low, close, volume):
    return dict(open=_r3(open_), high=_r3(max(high, open_, close)), low=_r3(min(low, open_, close)),
                close=_r3(close), volume=volume, suspendFlag=0)


def _market():
    """Weekday bars engineered around the C3 schedule so every hard path is exercised:
    a buy price guard and its next-session fill, a touch-only fill, a thin symbol that
    fills over several sessions, a first sell blocked by the -0.5% guard and completed by
    the -2% retry, and buys that wait for the late sale proceeds."""
    from app.oms.planner import session_schedule

    days = pd.bdate_range("2026-03-02", periods=48)
    cal = [d.strftime("%Y%m%d") for d in days]
    bars = {}
    for i, d in enumerate(cal):
        for j, s in enumerate(SYMBOLS):
            close = BASE[s] * (1 + 0.003 * math.sin(0.7 * i + j))
            open_ = close * (1 + 0.0008 * math.cos(i + j))
            bars[(d, s)] = _bar(open_, max(open_, close) * 1.002, min(open_, close) * 0.998, close,
                                600 if s == "518880.SH" else 3_000_000)
    signals = [cal[3], cal[24]]
    first, second = session_schedule(cal, signals[0], 3), session_schedule(cal, signals[1], 3)

    # Cycle 1 (initial build): first buy day.
    a = {s: bars[(first[0].trade_date, s)]["close"] for s in SYMBOLS}      # buy anchors = first sell-day close
    buys = [x.trade_date for x in first if x.phase == "BUY"]
    bars[(buys[0], "510300.SH")] = _bar(a["510300.SH"] * 1.012, a["510300.SH"] * 1.016, a["510300.SH"] * 1.008,
                                        a["510300.SH"] * 1.010, 3_000_000)          # above +0.5% all day
    bars[(buys[0], "518880.SH")] = _bar(a["518880.SH"] * 1.015, a["518880.SH"] * 1.016, a["518880.SH"] * 0.998,
                                        a["518880.SH"] * 1.002, 600)                # trades through +1% limit
    bars[(buys[1], "510300.SH")] = _bar(a["510300.SH"] * 1.001, a["510300.SH"] * 1.003, a["510300.SH"] * 0.997,
                                        a["510300.SH"] * 1.000, 3_000_000)

    # Cycle 2: 510300 is reduced; the first sell close is 1% under the signal close.
    sig = bars[(signals[1], "510300.SH")]["close"]
    sells = [x.trade_date for x in second if x.phase == "SELL"]
    assert len(sells) >= 2, "cycle 2 must contain a retry sell session"
    bars[(sells[0], "510300.SH")] = _bar(sig * 0.995, sig * 0.996, sig * 0.985, sig * 0.990, 3_000_000)
    bars[(sells[1], "510300.SH")] = _bar(sig * 0.990, sig * 0.991, sig * 0.982, sig * 0.985, 3_000_000)

    rows = [dict(date=pd.Timestamp(d), symbol=s, **b) for (d, s), b in sorted(bars.items())]
    raw = pd.DataFrame(rows)
    daily = {(r.date, r.symbol): r._asdict() for r in raw.itertuples(index=False)}
    close = raw.pivot(index="date", columns="symbol", values="close")
    weights = pd.DataFrame([[0.25, 0.55, 0.12, 0.08], [0.10, 0.62, 0.12, 0.16]], columns=SYMBOLS,
                           index=[pd.Timestamp(signals[0]), pd.Timestamp(signals[1])])
    return weights, daily, close, bars


class ServerAdapter:
    """LiveServerClient surface on top of the in-process FastAPI app."""

    def __init__(self, client):
        self.client, self.down = client, False

    def _check(self):
        if self.down:
            raise ConnectionError("server down")

    def post_oms_snapshot(self, payload):
        self._check()
        body = self.client.post("/oms/live/snapshot", json=payload, headers=LIVE).json()
        assert body["code"] == 0, body
        return body["data"]

    def get_oms_plan(self, alias, trade_date, phase):
        self._check()
        response = self.client.get("/oms/live/plan", params={"account_alias": alias, "trade_date": trade_date,
                                                             "phase": phase}, headers=LIVE)
        if response.status_code == 404:
            raise LookupError(phase)
        body = response.json()
        assert body["code"] == 0, body
        return body["data"]

    def post_oms_events(self, alias, events):
        self._check()
        body = self.client.post("/oms/live/events", json={"account_alias": alias, "events": events},
                                headers=LIVE).json()
        assert body["code"] == 0, body
        return body["data"]


class System:
    def __init__(self, tmp_path, bars):
        get_settings.cache_clear()
        _engine_for_url.cache_clear()
        self.settings = Settings(
            live_api_key="LIVE_KEY", live_client_id="hydra-live-client", live_account_aliases_csv=ALIAS,
            oms_live_enabled=True, stock_commission_rate=0.0001, stock_min_commission=5.0,
            stock_stamp_duty_sell=0.0, db_url=f"sqlite:///{tmp_path}/system.db", parquet_root=tmp_path / "data",
            plugins_dir=tmp_path / "plugins", strategies_file=tmp_path / "strategies.yaml", log_level="WARNING")
        self.client = TestClient(create_app(settings_override=self.settings))
        self.sf = make_session_factory(_engine_for_url(self.settings.db_url))
        with self.sf() as s:
            s.add(InstanceState(instance_id=INSTANCE, execution_domain="live", account_alias=ALIAS,
                                ledger_mode="attributed", virtual_cash=CAPITAL, virtual_positions={},
                                owned_symbols=SYMBOLS, last_update="x"))
            s.commit()
        self.service = get_oms_cycle_service(self.sf, self.settings)
        self.exchange = SimExchange(bars, cash=CAPITAL, positions={})
        self.server = ServerAdapter(self.client)
        self.journal_path = tmp_path / "agent" / "oms-agent.db"
        self.journal_path.parent.mkdir()
        self.agent = self.new_agent()
        self.trades = []

    def new_agent(self):
        """A fresh agent process over the same journal file (crash/restart)."""
        gateway = SimQMTGateway(self.exchange)
        gateway.connect()

        def sleep(seconds):
            later = self.exchange.now() + timedelta(seconds=seconds)
            self.exchange.set_clock(later.strftime("%Y%m%d"), later.strftime("%H%M%S"))

        return OmsAgent(account_alias=ALIAS, gateway=gateway, server=self.server,
                        journal=OmsJournal(self.journal_path, clock=self.exchange.now), clock=self.exchange.now,
                        symbols=SYMBOLS, sleep=sleep, spool_dir=self.journal_path.parent / "spool")

    def clock(self, day, hhmmss):
        self.exchange.set_clock(day, hhmmss)

    def cycle_open(self):
        with self.sf() as s:
            return s.query(OmsCycle).filter(OmsCycle.status.in_(("ACTIVE", "HELD", "PENDING_APPROVAL"))).count() > 0

    def run_day(self, day, signal=None, hooks=None):
        hooks = hooks or {}
        if self.cycle_open():
            self.clock(day, "090000")
            hooks.get("pre_buy", lambda: None)()
            self.agent.pre(day)
            self.clock(day, "091505")
            hooks.get("buy", lambda: None)()
            self.agent.execute(day, "BUY")
            hooks.get("after_buy", lambda: None)()
            self.clock(day, "144500")
            self.agent.pre(day)
            self.clock(day, "145500")
            self.agent.cancel_open()
            self.clock(day, "145705")
            hooks.get("sell", lambda: None)()
            self.agent.execute(day, "SELL")
            hooks.get("after_sell", lambda: None)()
            self.clock(day, "150500")
            hooks.get("eod", lambda: self.agent.eod())()
            self.trades += [dict(t, date=day) for t in self.exchange.trades()]
        if signal is not None:
            weights, closes = signal
            self.service.publish_target(instance_id=INSTANCE, account_alias=ALIAS, signal_date=day,
                                        weights=weights, signal_closes=closes,
                                        calendar=sorted({d for d, _ in self.exchange.bars}),
                                        source_sha256=f"synthetic-{day}".ljust(64, "0"),
                                        now=f"{day[:4]}-{day[4:6]}-{day[6:]}T20:00:00+08:00")
            with self.sf() as s:
                cycle_id = s.query(OmsCycle).filter_by(status="PENDING_APPROVAL").one().cycle_id
            self.service.approve(cycle_id, "system-replay", "n")

    def ledger(self):
        with self.sf() as s:
            inst = s.get(InstanceState, INSTANCE)
            return {k: int(v) for k, v in (inst.virtual_positions or {}).items() if int(v)}, float(inst.virtual_cash)


def _run_system(tmp_path, hooks_by_day=None):
    weights, daily, close, bars = _market()
    system = System(tmp_path, bars)
    signals = {d.strftime("%Y%m%d"): ({s: float(w) for s, w in weights.loc[d].items()},
                                      {s: float(close.loc[d, s]) for s in SYMBOLS}) for d in weights.index}
    days = [d.strftime("%Y%m%d") for d in close.index]
    for day in days[days.index(min(signals)):]:
        system.run_day(day, signals.get(day), (hooks_by_day or {}).get(day))
    return system, (weights, daily, close)


def _replay(weights, daily, close, with_events=False):
    c3 = policy_replay.Policy("C3", lot="nearest", size_factor=1.001, buy_bps=Policy().buy_bps, touch=True,
                              sell_schedule=(50., 200.))
    _, hist, _, fills, events = policy_replay.run(weights, daily, close, [], c3, capital=CAPITAL, slip_bps=5.,
                                                  participation=.01)
    return (hist, fills, events) if with_events else (hist, fills)


def _fill_key(date, symbol, side, quantity, price):
    return (date, symbol, side, int(quantity), round(float(price), 3))


def test_system_reproduces_research_replay_exactly(tmp_path):
    system, data = _run_system(tmp_path)
    hist, fills, events = _replay(*data, with_events=True)
    # Coverage: the engineered market must hit every hard path, or this test proves little.
    reasons = set(zip(events.side, events.reason))
    assert {("BUY", "PRICE_GUARD"), ("BUY", "CAPACITY"), ("SELL", "PRICE_GUARD")} <= reasons, reasons
    assert len(set(fills.date)) >= 5
    replay_fills = Counter(_fill_key(f.date.replace("-", ""), f.symbol, f.direction, f.quantity, f.price)
                           for f in fills.itertuples())
    sides = {23: "BUY", 24: "SELL"}
    system_fills = Counter(_fill_key(t["date"], t["stock_code"], sides[t["order_type"]], t["traded_volume"],
                                     t["traded_price"]) for t in system.trades)
    assert replay_fills, "the synthetic market must produce trades"
    assert system_fills == replay_fills
    positions, cash = system.ledger()
    broker = {s: p["volume"] for s, p in system.exchange.positions().items() if p["volume"]}
    assert positions == broker
    assert cash == pytest.approx(system.exchange.asset()["cash"], abs=1e-6)
    assert cash == pytest.approx(float(hist.cash.iloc[-1]), abs=1e-6)
    assert not system.cycle_open()


def _assert_single_order_per_remark_and_consistent(system):
    remarks = Counter(t["order_remark"] for t in system.exchange.orders())
    assert all(count == 1 for count in remarks.values()), remarks
    positions, _ = system.ledger()
    broker = {s: p["volume"] for s, p in system.exchange.positions().items() if p["volume"]}
    assert positions == broker


def _cycle2():
    """Session days of the second cycle, from the same calendar the system uses."""
    from app.oms.planner import session_schedule
    weights, _, close, _ = _market()
    cal = [d.strftime("%Y%m%d") for d in close.index]
    sched = session_schedule(cal, weights.index[1].strftime("%Y%m%d"), 3)
    return ([x.trade_date for x in sched if x.phase == "SELL"], [x.trade_date for x in sched if x.phase == "BUY"])


def _drive(tmp_path, hooks_for):
    """Run the whole replay; ``hooks_for(system)`` returns {day: {step: callable}}."""
    weights, daily, close, bars = _market()
    system = System(tmp_path, bars)
    hooks = hooks_for(system)
    signals = {d.strftime("%Y%m%d"): ({s: float(w) for s, w in weights.loc[d].items()},
                                      {s: float(close.loc[d, s]) for s in SYMBOLS}) for d in weights.index}
    days = [d.strftime("%Y%m%d") for d in close.index]
    for d in days[days.index(min(signals)):]:
        system.run_day(d, signals.get(d), hooks.get(d))
    hist, _ = _replay(weights, daily, close)
    return system, hist


def test_server_outage_during_sell_session_replays_events_and_matches(tmp_path):
    sell_day = _cycle2()[0][0]
    seen = {}

    def hooks_for(system):
        def down():
            system.server.down = True

        def up():
            seen["outbox_while_down"] = len(system.agent.journal.outbox())
            seen["orders_while_down"] = len(system.exchange.orders())
            system.server.down = False
        return {sell_day: {"sell": down, "after_sell": up}}

    system, hist = _drive(tmp_path, hooks_for)
    assert seen["outbox_while_down"] > 0 and seen["orders_while_down"] > 0, seen   # fault really happened
    assert system.agent.journal.outbox() == []
    _assert_single_order_per_remark_and_consistent(system)
    assert system.ledger()[1] == pytest.approx(float(hist.cash.iloc[-1]), abs=1e-6)


def test_agent_crash_after_submitting_never_double_submits(tmp_path):
    buy_day = _cycle2()[1][0]
    seen = {}

    def hooks_for(system):
        def crash_and_restart():
            plan = system.agent.fetch_plan(buy_day, "BUY")
            coid = plan["orders"][0]["client_order_id"]
            # The first process journals SUBMITTING and dies before calling QMT.
            system.agent.journal.mark(coid, "SUBMITTING", None, None)
            seen["coid"] = coid
            system.agent = system.new_agent()
        return {buy_day: {"buy": crash_and_restart}}

    system, _ = _drive(tmp_path, hooks_for)
    assert all(o["order_remark"] != seen["coid"] for o in system.exchange.orders())   # never sent
    from app.oms.models import OmsOrder
    with system.sf() as s:
        assert s.get(OmsOrder, seen["coid"]).state == "NOT_SUBMITTED"                  # finalized at EOD
    _assert_single_order_per_remark_and_consistent(system)


def test_unknown_submit_resolves_by_remark_without_resubmission(tmp_path):
    sell_day = _cycle2()[0][0]
    seen = {}

    def hooks_for(system):
        def fault_on():
            system.exchange.faults.add("submit_raises_after_accept")

        def fault_off_and_retry():
            seen["unknown"] = [i for i in (system.agent.journal.intent(o["client_order_id"])
                                           for o in system.agent.fetch_plan(sell_day, "SELL")["orders"])
                               if i and i["state"] == "UNKNOWN"]
            system.exchange.faults.discard("submit_raises_after_accept")
            system.agent.execute(sell_day, "SELL")
        return {sell_day: {"sell": fault_on, "after_sell": fault_off_and_retry}}

    system, hist = _drive(tmp_path, hooks_for)
    assert seen["unknown"], "the fault must leave an UNKNOWN intent"
    for intent in seen["unknown"]:
        assert system.agent.journal.intent(intent["client_order_id"])["state"] == "ACKED"
    _assert_single_order_per_remark_and_consistent(system)
    assert system.ledger()[1] == pytest.approx(float(hist.cash.iloc[-1]), abs=1e-6)


def test_trade_query_hang_and_duplicate_event_upload_change_nothing(tmp_path):
    def hooks_for(system):
        system.exchange.faults.add("trades_hang")
        original = system.server.post_oms_events

        def twice(alias, events):
            original(alias, events)
            return original(alias, events)
        system.server.post_oms_events = twice
        return {}

    system, hist = _drive(tmp_path, hooks_for)
    _assert_single_order_per_remark_and_consistent(system)
    assert system.ledger()[1] == pytest.approx(float(hist.cash.iloc[-1]), abs=1e-6)


def test_missing_eod_snapshot_holds_next_session(tmp_path):
    buy_day = _cycle2()[1][0]

    def hooks_for(system):
        return {buy_day: {"eod": lambda: None}}

    system, _ = _drive(tmp_path, hooks_for)
    with system.sf() as s:
        cycle = s.query(OmsCycle).filter_by(cycle_no=2).one()
        # That day's fills were never reported and QMT no longer lists them: the next PRE holds.
        assert cycle.status == "HELD"
        recon = s.query(OmsReconciliation).filter_by(passed=False).first()
        assert recon is not None and recon.discrepancies[0]["type"] == "POSITION_MISMATCH"
    remarks = Counter(t["order_remark"] for t in system.exchange.orders())
    assert all(count == 1 for count in remarks.values())
