"""OMS agent: journal before submit, never resubmit an unknown, cap buys at live positions."""
from datetime import datetime, timedelta

import pytest

from live_client.oms_agent import OmsAgent
from live_client.oms_journal import OmsJournal
from live_client.sim_exchange import SimExchange, SimQMTGateway

ALIAS = "hydra-live"
D1, D2 = "20261008", "20261009"
BARS = {
    (D1, "510300.SH"): dict(open=4.62, high=4.66, low=4.58, close=4.60, volume=5_000_000),
    (D1, "513100.SH"): dict(open=2.20, high=2.21, low=2.19, close=2.20, volume=1_000_000),
    (D2, "510300.SH"): dict(open=4.61, high=4.65, low=4.59, close=4.62, volume=5_000_000),
    (D2, "513100.SH"): dict(open=2.21, high=2.24, low=2.20, close=2.22, volume=1_000_000),
}


class FakeServer:
    def __init__(self, plans):
        self.plans, self.snapshots, self.events, self.down = plans, [], [], False
        self.manual, self.acks, self.fail_ack = [], [], False

    def _check(self):
        if self.down:
            raise ConnectionError("server down")

    def post_oms_snapshot(self, payload):
        self._check()
        self.snapshots.append(payload)
        return {"reconciliation": {"passed": True}}

    def get_oms_plan(self, alias, trade_date, phase):
        self._check()
        if (trade_date, phase) not in self.plans:
            raise LookupError(phase)
        return self.plans[(trade_date, phase)]

    def post_oms_events(self, alias, events):
        self._check()
        self.events.extend(events)
        return {"applied": len(events)}

    def get_oms_manual_pending(self, alias, trade_date):
        self._check()
        done = {a["instruction_id"] for a in self.acks}
        return {"trade_date": trade_date, "instructions": [m for m in self.manual if m["instruction_id"] not in done]}

    def post_oms_manual_ack(self, alias, results):
        self._check()
        if self.fail_ack:
            raise ConnectionError("ack lost")
        self.acks.extend(results)
        return {"updated": len(results)}


def _plan(day, phase, orders, frozen, executable=True, seq=1):
    return {"account_alias": ALIAS, "cycle_id": "C00001", "session_id": f"C00001:{seq}", "trade_date": day,
            "phase": phase, "executable": executable, "plan_sha256": f"{day}{phase}".ljust(64, "0"),
            "frozen_target": frozen, "orders": orders}


SELL = {"client_order_id": "H000010101", "symbol": "510300.SH", "side": "SELL", "quantity": 1800, "limit_price": 4.577}
BUY = {"client_order_id": "H000010201", "symbol": "513100.SH", "side": "BUY", "quantity": 1000, "limit_price": 2.233}
FROZEN = {"510300.SH": 1200, "513100.SH": 1000}


def _setup(tmp_path, plans, *, positions=None, cash=100000.0, at=(D1, "145705")):
    exchange = SimExchange(BARS, cash=cash, positions=positions or {"510300.SH": 3000})
    exchange.set_clock(*at)
    gateway = SimQMTGateway(exchange)
    gateway.connect()
    server = FakeServer(plans)
    journal = OmsJournal(tmp_path / "oms-agent.db", clock=exchange.now)

    def sleep(seconds):
        later = exchange.now() + timedelta(seconds=seconds)
        exchange.set_clock(later.strftime("%Y%m%d"), later.strftime("%H%M%S"))

    agent = OmsAgent(account_alias=ALIAS, gateway=gateway, server=server, journal=journal, clock=exchange.now,
                     symbols=["510300.SH", "513100.SH"], sleep=sleep, spool_dir=tmp_path / "spool")
    return agent, exchange, server, journal


def test_sell_outside_window_is_refused(tmp_path):
    agent, exchange, _, _ = _setup(tmp_path, {(D1, "SELL"): _plan(D1, "SELL", [SELL], FROZEN)}, at=(D1, "145000"))
    assert agent.execute(D1, "SELL")["status"] == "OUTSIDE_WINDOW"
    assert exchange.orders() == []


def test_sell_in_closing_auction_fills_at_close_and_eod_reports_it(tmp_path):
    agent, exchange, server, journal = _setup(tmp_path, {(D1, "SELL"): _plan(D1, "SELL", [SELL], FROZEN)})
    out = agent.execute(D1, "SELL")
    assert out["status"] == "EXECUTED" and journal.intent("H000010101")["state"] == "ACKED"
    assert [e["kind"] for e in server.events] == ["SUBMIT_STARTED", "ACKED"]
    exchange.set_clock(D1, "150500")
    agent.eod()
    reported = server.snapshots[-1]
    assert reported["kind"] == "EOD"
    order = [o for o in reported["orders"] if o["remark"] == "H000010101"][0]
    assert order["status"] == 56 and order["traded_volume"] == 1800 and order["side"] == "SELL"
    assert reported["positions"]["510300.SH"] == 1200


def test_unknown_submit_is_never_resubmitted_and_resolves_by_remark(tmp_path):
    agent, exchange, _, journal = _setup(tmp_path, {(D1, "SELL"): _plan(D1, "SELL", [SELL], FROZEN)})
    exchange.faults.add("submit_raises_after_accept")
    agent.execute(D1, "SELL")
    assert journal.intent("H000010101")["state"] == "UNKNOWN"
    exchange.faults.discard("submit_raises_after_accept")
    agent.execute(D1, "SELL")
    assert len(exchange.orders()) == 1
    assert journal.intent("H000010101")["state"] == "ACKED"


def test_server_down_uses_cached_plan_and_replays_events_later(tmp_path):
    plans = {(D1, "SELL"): _plan(D1, "SELL", [SELL], FROZEN)}
    agent, exchange, server, journal = _setup(tmp_path, plans, at=(D1, "144500"))
    agent.pre(D1)                       # caches the plan while the server is up
    server.down = True
    exchange.set_clock(D1, "145705")
    assert agent.execute(D1, "SELL")["status"] == "EXECUTED"
    assert len(exchange.orders()) == 1 and server.events == []
    assert len(journal.outbox()) == 2
    server.down = False
    agent.flush_events()
    assert [e["kind"] for e in server.events] == ["SUBMIT_STARTED", "ACKED"] and journal.outbox() == []


def test_local_hold_blocks_execution(tmp_path):
    agent, exchange, _, _ = _setup(tmp_path, {(D1, "SELL"): _plan(D1, "SELL", [SELL], FROZEN)})
    (tmp_path / "HOLD").write_text("manual hold")
    assert agent.execute(D1, "SELL")["status"] == "HOLD_LOCAL"
    assert exchange.orders() == []


def test_buy_is_capped_by_live_position_against_frozen_target(tmp_path):
    plans = {(D2, "BUY"): _plan(D2, "BUY", [BUY], FROZEN, seq=2)}
    # A late fill already brought 600 shares in; only 400 more fit under the frozen target.
    agent, exchange, _, _ = _setup(tmp_path, plans, positions={"510300.SH": 1200, "513100.SH": 600},
                                   at=(D2, "091505"))
    agent.execute(D2, "BUY")
    assert [o["order_volume"] for o in exchange.orders()] == [400]


def test_not_executable_plan_and_dry_run_never_submit(tmp_path):
    plans = {(D1, "SELL"): _plan(D1, "SELL", [SELL], FROZEN, executable=False)}
    agent, exchange, _, _ = _setup(tmp_path, plans)
    assert agent.execute(D1, "SELL")["status"] == "NOT_EXECUTABLE"
    dry = agent.execute(D1, "SELL", dry_run=True)
    assert dry["status"] == "DRY_RUN" and dry["orders"][0]["quantity"] == 1800
    assert exchange.orders() == []


def test_cancel_open_only_touches_our_buy_orders_and_refuses_after_1457(tmp_path):
    plans = {(D2, "BUY"): _plan(D2, "BUY", [dict(BUY, limit_price=2.15)], FROZEN, seq=2)}
    agent, exchange, _, _ = _setup(tmp_path, plans, positions={"510300.SH": 1200}, at=(D2, "091505"))
    agent.execute(D2, "BUY")                                   # 2.15 never trades: rests all day
    manual = exchange.order_stock("510300.SH", "BUY", 100, 4.50, "manual")
    exchange.set_clock(D2, "145500")
    out = agent.cancel_open()
    assert out["requested"] == ["H000010201"]
    statuses = {o["order_remark"]: o["order_status"] for o in exchange.orders()}
    assert statuses["manual"] == 50 and statuses["H000010201"] in (51, 54)
    exchange.set_clock(D2, "145700")
    assert agent.cancel_open()["status"] == "TOO_LATE"
    assert manual > 0


def test_spooled_snapshots_upload_in_order_once_server_returns(tmp_path):
    agent, exchange, server, _ = _setup(tmp_path, {}, at=(D1, "150500"))
    server.down = True
    agent.eod()
    exchange.set_clock(D1, "153000")
    agent.eod()
    assert server.snapshots == [] and len(list((tmp_path / "spool").glob("snapshot-*.json"))) == 2
    server.down = False
    out = agent.upload_spool()
    assert out["failed"] is None and len(out["uploaded"]) == 2
    assert [s["taken_at"][11:19] for s in server.snapshots] == ["15:05:00", "15:30:00"]
    assert list((tmp_path / "spool").glob("snapshot-*.json")) == []


def test_agent_affordability_matches_planner_cost_exactly(tmp_path):
    """Bug 5: the agent trimmed a lot the planner had funded (different cost formula)."""
    from app.oms.planner import _cost
    order = dict(BUY, quantity=1000, limit_price=2.233)
    cash = _cost({"limit_price": 2.233}, 1000) / 100          # exactly what the planner reserved
    plans = {(D2, "BUY"): _plan(D2, "BUY", [order], {"510300.SH": 1200, "513100.SH": 1000}, seq=2)}
    agent, exchange, _, _ = _setup(tmp_path, plans, positions={"510300.SH": 1200}, cash=cash, at=(D2, "091505"))
    agent.execute(D2, "BUY")
    assert [o["order_volume"] for o in exchange.orders()] == [1000]


MANUAL_BUY = {"instruction_id": "E261008001", "kind": "ORDER", "client_order_id": "E261008001",
              "broker_order_id": None, "symbol": "513100.SH", "side": "BUY", "quantity": 500, "limit_price": 2.21}


def _manual_setup(tmp_path, at=(D1, "100000"), plans=None):
    agent, exchange, server, journal = _setup(tmp_path, plans or {}, at=at)
    server.manual, server.acks = [], []
    return agent, exchange, server, journal


def test_manual_order_is_submitted_with_e_remark_and_acked(tmp_path):
    agent, exchange, server, journal = _manual_setup(tmp_path)
    server.manual = [MANUAL_BUY]
    out = agent.run_manual(D1)
    assert out["results"][0]["status"] == "SUBMITTED"
    assert [o["order_remark"] for o in exchange.orders()] == ["E261008001"]
    assert server.acks[0]["instruction_id"] == "E261008001" and journal.intent("E261008001")["state"] == "ACKED"
    assert agent.run_manual(D1)["status"] == "NOTHING_PENDING"


def test_lost_ack_never_resubmits_a_manual_order(tmp_path):
    agent, exchange, server, _ = _manual_setup(tmp_path)
    server.manual, server.fail_ack = [MANUAL_BUY], True
    agent.run_manual(D1)
    server.fail_ack = False
    out = agent.run_manual(D1)                                  # still pending on the server side
    assert out["results"][0]["status"] == "SUBMITTED" and len(exchange.orders()) == 1


def test_manual_cancel_by_broker_id_and_unknown_target_fails(tmp_path):
    agent, exchange, server, _ = _manual_setup(tmp_path)
    resting = exchange.order_stock("510300.SH", "BUY", 100, 4.50, "manual-in-qmt-gui")
    server.manual = [{"instruction_id": "X261008001", "kind": "CANCEL", "client_order_id": None,
                      "broker_order_id": str(resting)},
                     {"instruction_id": "X261008002", "kind": "CANCEL", "client_order_id": "H000019999",
                      "broker_order_id": None}]
    out = agent.run_manual(D1)
    statuses = {r["instruction_id"]: r["status"] for r in out["results"]}
    assert statuses == {"X261008001": "CANCEL_REQUESTED", "X261008002": "FAILED"}
    assert exchange.orders()[0]["order_status"] in (51, 54)


def test_local_hold_pauses_manual_orders_but_still_cancels(tmp_path):
    agent, exchange, server, _ = _manual_setup(tmp_path)
    resting = exchange.order_stock("510300.SH", "BUY", 100, 4.50, "manual-in-qmt-gui")
    server.manual = [MANUAL_BUY, {"instruction_id": "X261008001", "kind": "CANCEL", "client_order_id": None,
                                  "broker_order_id": str(resting)}]
    (tmp_path / "HOLD").write_text("hold")
    out = agent.run_manual(D1)
    assert out["held_back_by_local_hold"] == ["E261008001"]
    assert [r["instruction_id"] for r in server.acks] == ["X261008001"]      # the order stays pending
    assert all(o["order_remark"] != "E261008001" for o in exchange.orders())


def test_manual_outside_window_is_refused(tmp_path):
    agent, exchange, server, _ = _manual_setup(tmp_path, at=(D1, "150100"))
    server.manual = [MANUAL_BUY]
    assert agent.run_manual(D1)["status"] == "OUTSIDE_WINDOW" and exchange.orders() == []


def test_manual_plans_never_shadow_the_cycle_plan_cache(tmp_path):
    plans = {(D1, "SELL"): _plan(D1, "SELL", [SELL], FROZEN)}
    agent, _, server, journal = _manual_setup(tmp_path, plans=plans)
    agent.fetch_plan(D1, "SELL")
    server.manual = [MANUAL_BUY]
    agent.run_manual(D1)
    assert journal.plan(D1, "SELL")["session_id"] == "C00001:1"
