"""OMS session agent: run server plans against QMT, journal before every submit, report facts.

The agent never plans. It submits the server's session plan inside a fixed window,
caps each order at what the live account can actually take, never resubmits an order
whose outcome is unknown, and uploads broker snapshots and journal events. When the
server is unreachable it keeps working from the cached plan and replays events later.
"""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import sys
import time as _time
from typing import Callable

from live_client.gateway import QMT_STOCK_BUY, QMT_STOCK_SELL

logger = logging.getLogger("hydra.oms_agent")

CHINA = timezone(timedelta(hours=8))
WINDOWS = {"SELL": (time(14, 57, 0), time(14, 59, 30)), "BUY": (time(9, 15, 0), time(9, 19, 30))}
SUBMIT_AT = {"SELL": time(14, 57, 5), "BUY": time(9, 15, 5)}
CANCEL_DEADLINE = time(14, 57, 0)
OPEN_STATUSES = (48, 49, 50, 55)
CANCELLABLE = (48, 49, 50, 55)
MANUAL_WINDOW = (time(9, 15, 0), time(14, 59, 30))
INTRADAY_WINDOW = (time(9, 15, 0), time(15, 1, 0))    # status snapshots from the every-minute task
# Seconds a step waits for the account lock. A cycle step can start in the same second as the
# every-minute intraday task (or while the cancel step is still confirming), so it waits;
# the intraday task never waits, it skips that minute.
LOCK_WAIT_SECONDS = {"pre": 120., "buy": 240., "sell": 150., "cancel": 100., "eod": 120., "upload-spool": 120.,
                     "intraday": 0.}
CYCLE_REMARK = re.compile(r"^H\d{9}$")       # orders of a rebalance cycle
OUR_REMARK = re.compile(r"^[HE]\d{9}$")      # cycle orders plus dashboard manual (E) orders
LOT = 100
MIN_FEE = 5.0
_SIDES = {QMT_STOCK_BUY: "BUY", QMT_STOCK_SELL: "SELL"}


def _buy_cost(quantity: int, limit: float) -> float:
    """Same reservation as app.oms.planner.allocate_buys, so the agent never trims a lot the plan funded."""
    notional = quantity * limit
    return notional + max(MIN_FEE, notional / 1000)


class OmsAgent:
    def __init__(self, *, account_alias: str, gateway, server, journal, clock: Callable[[], datetime],
                 symbols, sleep: Callable[[float], None] = _time.sleep, spool_dir: Path | None = None):
        self.account_alias = account_alias
        self.gateway = gateway
        self.server = server
        self.journal = journal
        self.clock = clock
        self.symbols = sorted(symbols)
        self.sleep = sleep
        self.spool_dir = spool_dir or journal.path.parent / "oms-spool"

    # ── facts ─────────────────────────────────────────────────────────────
    def snapshot(self, kind: str, spool: bool = True) -> dict:
        now = self.clock().astimezone(CHINA)
        account = self.gateway.account_snapshot()
        orders = [self._order_wire(o) for o in self.gateway.day_orders()]
        payload = {
            "account_alias": self.account_alias, "kind": kind, "trade_date": now.strftime("%Y%m%d"),
            "taken_at": now.isoformat(), "available_cash": float(account.available_cash),
            "total_asset": float(account.total_asset),
            "positions": {s: int(q) for s, q in account.positions.items() if int(q)},
            "sellable": {s: int(q) for s, q in account.sellable_positions.items() if int(q)},
            "orders": [o for o in orders if o is not None],
            "trades": self.gateway.day_trades(),
            "quotes": {s: {"last_price": float(q["last_price"]), "is_trading": bool(q.get("is_trading", True))}
                       for s, q in self.gateway.quotes(self.symbols).items()},
        }
        try:
            result = self.server.post_oms_snapshot(payload)
        except Exception as exc:  # keep the evidence; the server reconciles it once uploaded
            if spool:
                path = self._spool(payload)
                logger.error("snapshot upload failed (%s); spooled to %s", exc, path)
                result = {"uploaded": False, "spooled": str(path)}
            else:
                logger.warning("%s snapshot upload failed (%s); not spooled", kind, exc)
                result = {"uploaded": False}
        self.flush_events()
        return result

    @staticmethod
    def _order_wire(order) -> dict | None:
        side = _SIDES.get(int(order.order_type or 0))
        if side is None:
            logger.warning("skipping broker order %s with unmapped order_type %s", order.order_id, order.order_type)
            return None
        return {"broker_order_id": str(order.order_id), "symbol": order.stock_code, "side": side,
                "quantity": int(order.order_volume), "price": float(order.price),
                "traded_volume": int(order.traded_volume), "traded_price": float(order.traded_price or 0),
                "status": int(order.order_status), "remark": (order.order_remark or "").strip(),
                "status_msg": order.status_msg or ""}

    def _spool(self, payload: dict) -> Path:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        stamp = payload["taken_at"].replace(":", "").replace("+", "p")
        path = self.spool_dir / f"snapshot-{payload['kind']}-{stamp}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def flush_events(self) -> int:
        events = self.journal.outbox()
        if not events:
            return 0
        try:
            self.server.post_oms_events(self.account_alias, events)
        except Exception as exc:
            logger.error("event upload failed (%s); %d events stay in the outbox", exc, len(events))
            return -1
        # The server de-duplicates by event_id and records rejected ones, so all are delivered.
        self.journal.acknowledge([e["event_id"] for e in events])
        return len(events)

    # ── plans ─────────────────────────────────────────────────────────────
    def fetch_plan(self, trade_date: str, phase: str) -> dict | None:
        try:
            plan = self.server.get_oms_plan(self.account_alias, trade_date, phase)
        except LookupError:
            return self.journal.plan(trade_date, phase)
        except Exception as exc:
            logger.error("plan fetch failed (%s); using the cached plan if any", exc)
            return self.journal.plan(trade_date, phase)
        self.journal.cache_plan(plan)
        return self.journal.plan(trade_date, phase)

    def pre(self, trade_date: str) -> dict:
        result = self.snapshot("PRE")
        cached = {phase: self.fetch_plan(trade_date, phase) is not None for phase in ("BUY", "SELL")}
        return {"snapshot": result, "cached_plans": cached}

    # ── execution ─────────────────────────────────────────────────────────
    def execute(self, trade_date: str, phase: str, dry_run: bool = False) -> dict:
        now = self.clock().astimezone(CHINA)
        if self.journal.hold_flag():
            return {"status": "HOLD_LOCAL"}
        plan = self.fetch_plan(trade_date, phase)
        if plan is None:
            return {"status": "NO_PLAN"}
        if plan["trade_date"] != now.strftime("%Y%m%d"):
            return {"status": "WRONG_DATE", "plan_date": plan["trade_date"]}
        if not dry_run:
            if not plan["executable"]:
                return {"status": "NOT_EXECUTABLE"}
            start, end = WINDOWS[phase]
            if not start <= now.time() <= end:
                return {"status": "OUTSIDE_WINDOW", "window": [str(start), str(end)]}
        account = self.gateway.account_snapshot()
        at_broker = {}
        for order in self.gateway.day_orders():
            remark = (order.order_remark or "").strip()
            if OUR_REMARK.match(remark):
                at_broker.setdefault(remark, order)
        cash = float(account.available_cash)
        results = []
        for item in plan["orders"]:
            coid, symbol, side = item["client_order_id"], item["symbol"], item["side"]
            limit = float(item["limit_price"])
            intent = self.journal.intent(coid)
            if coid in at_broker:
                if intent is None or intent["state"] in ("SUBMITTING", "UNKNOWN"):
                    self.journal.mark(coid, "ACKED", str(at_broker[coid].order_id), "found at broker by remark")
                results.append({"client_order_id": coid, "status": "ALREADY_AT_BROKER"})
                continue
            if intent is not None:
                # SUBMITTING/UNKNOWN without broker evidence, ACKED or REJECTED: never submit again.
                results.append({"client_order_id": coid, "status": f"SKIPPED_{intent['state']}"})
                continue
            quantity = int(item["quantity"])
            if side == "BUY":
                held = int(account.positions.get(symbol, 0))
                room = max(0, int(plan["frozen_target"].get(symbol, 0)) - held)
                quantity = min(quantity, room) // LOT * LOT
                while quantity > 0 and _buy_cost(quantity, limit) > cash:
                    quantity -= LOT
            else:
                quantity = min(quantity, int(account.sellable_positions.get(symbol, 0)))
            if quantity <= 0:
                results.append({"client_order_id": coid, "status": "SKIPPED_NO_ROOM"})
                continue
            if dry_run:
                results.append({"client_order_id": coid, "status": "DRY_RUN", "quantity": quantity})
                continue
            self.journal.mark(coid, "SUBMITTING", None, None)
            outcome = self.gateway.submit_limit(symbol=symbol, side=side, quantity=quantity, limit_price=limit,
                                                remark=coid)
            if outcome.status == "SUBMITTED":
                self.journal.mark(coid, "ACKED", outcome.local_order_id, None)
                if side == "BUY":
                    cash -= _buy_cost(quantity, limit)
            elif outcome.status == "REJECTED":
                self.journal.mark(coid, "REJECTED", None, outcome.detail)
            else:
                self.journal.mark(coid, "UNKNOWN", None, outcome.detail)
            results.append({"client_order_id": coid, "status": outcome.status, "quantity": quantity})
        self.flush_events()
        return {"status": "DRY_RUN" if dry_run else "EXECUTED", "orders": results}

    def cancel_open(self, wait_seconds: float = 60., step: float = 5.) -> dict:
        if self.clock().astimezone(CHINA).time() >= CANCEL_DEADLINE:
            return {"status": "TOO_LATE"}
        requested = []
        for order in self.gateway.day_orders():
            remark = (order.order_remark or "").strip()
            if (CYCLE_REMARK.match(remark) and _SIDES.get(int(order.order_type or 0)) == "BUY"
                    and int(order.order_status) in OPEN_STATUSES):
                self.gateway.cancel_order(int(order.order_id))
                requested.append(remark)
        waited = 0.
        while requested and waited < wait_seconds:
            open_left = [o for o in self.gateway.day_orders()
                         if (o.order_remark or "").strip() in requested and int(o.order_status) not in (53, 54, 56, 57)]
            if not open_left or self.clock().astimezone(CHINA).time() >= CANCEL_DEADLINE:
                break
            self.sleep(step)
            waited += step
        return {"status": "REQUESTED", "requested": requested}

    def eod(self) -> dict:
        result = self.snapshot("EOD")
        return {**result, "cached_upcoming": self.cache_upcoming()}

    def cache_upcoming(self) -> list[str]:
        """Cache the plans of later sessions after the EOD snapshot (the server plans the next
        session from it), so a server outage the next morning does not stop them. A fetch the
        next morning refreshes ``executable``; the local HOLD file stops them regardless."""
        today = self.clock().astimezone(CHINA).strftime("%Y%m%d")
        try:
            sessions = self.server.get_oms_status(self.account_alias).get("sessions") or []
        except Exception as exc:
            logger.error("status unavailable (%s); upcoming plans not cached", exc)
            return []
        cached = []
        for item in sessions:
            if item["trade_date"] <= today or item["status"] != "PLANNED":
                continue
            try:
                if self.fetch_plan(item["trade_date"], item["phase"]) is not None:
                    cached.append(f"{item['trade_date']}:{item['phase']}")
            except Exception as exc:  # e.g. a journal conflict: keep the EOD result and report it
                logger.error("plan %s %s not cached: %s", item["trade_date"], item["phase"], exc)
        return cached

    # ── every minute: dashboard instructions and status ───────────────────
    def intraday(self, trade_date: str) -> dict:
        """The every-minute task: dashboard instructions, then one status snapshot.

        Status snapshots never drive orders (the next plan comes from the EOD snapshot);
        they keep the server's view of acks and fills current through the day. They are
        not spooled while the server is down, because the PRE/EOD snapshots carry the evidence.
        """
        manual = self.run_manual(trade_date)
        now = self.clock().astimezone(CHINA).time()
        if not INTRADAY_WINDOW[0] <= now <= INTRADAY_WINDOW[1]:
            return {"manual": manual, "snapshot": None}
        return {"manual": manual, "snapshot": self.snapshot("ADHOC", spool=False)}

    def run_manual(self, trade_date: str) -> dict:
        """Execute today's pending dashboard orders and cancels, then report back.

        Orders: journal first, never resubmit, capped by sellable shares / available cash.
        Cancels: by our client order id or the broker order id. A local HOLD file pauses
        manual orders (they stay pending) but cancels still run, since they only cut risk.
        """
        now = self.clock().astimezone(CHINA)
        if not MANUAL_WINDOW[0] <= now.time() <= MANUAL_WINDOW[1]:
            return {"status": "OUTSIDE_WINDOW"}
        try:
            pending = self.server.get_oms_manual_pending(self.account_alias, trade_date)["instructions"]
        except Exception as exc:
            logger.error("manual instructions unavailable: %s", exc)
            return {"status": "SERVER_UNAVAILABLE"}
        if not pending:
            return {"status": "NOTHING_PENDING"}
        hold = self.journal.hold_flag()
        account = self.gateway.account_snapshot()
        orders = self.gateway.day_orders()
        by_remark = {}
        for order in orders:
            by_remark.setdefault((order.order_remark or "").strip(), order)
        by_broker_id = {str(order.order_id): order for order in orders}
        cash = float(account.available_cash)
        results, held_back = [], []
        for item in pending:
            if item["kind"] == "CANCEL":
                results.append(self._manual_cancel(item, by_remark, by_broker_id))
            elif hold:
                held_back.append(item["instruction_id"])
            else:
                outcome, cost = self._manual_order(item, trade_date, account, by_remark, cash)
                cash -= cost
                results.append(outcome)
        if results:
            try:
                self.server.post_oms_manual_ack(self.account_alias, results)
            except Exception as exc:  # orders are journaled; the next run re-reports from the journal
                logger.error("manual ack failed: %s", exc)
        self.flush_events()
        return {"status": "DONE", "results": results, "held_back_by_local_hold": held_back}

    def _manual_cancel(self, item, by_remark, by_broker_id) -> dict:
        target = (by_remark.get(item["client_order_id"]) if item.get("client_order_id")
                  else by_broker_id.get(str(item.get("broker_order_id"))))
        base = {"instruction_id": item["instruction_id"]}
        if target is None:
            return {**base, "status": "FAILED", "detail": "order not found among today's broker orders"}
        if int(target.order_status) not in CANCELLABLE:
            return {**base, "status": "FAILED", "detail": f"order is not open (QMT status {target.order_status})"}
        try:
            self.gateway.cancel_order(int(target.order_id))
        except Exception as exc:
            return {**base, "status": "FAILED", "detail": f"cancel refused: {exc}"}
        return {**base, "status": "CANCEL_REQUESTED", "detail": f"broker order {target.order_id}"}

    def _manual_order(self, item, trade_date, account, by_remark, cash) -> tuple[dict, float]:
        coid, symbol, side = item["client_order_id"], item["symbol"], item["side"]
        limit, quantity = float(item["limit_price"]), int(item["quantity"])
        base = {"instruction_id": item["instruction_id"]}
        order = {"client_order_id": coid, "symbol": symbol, "side": side, "quantity": quantity, "limit_price": limit}
        self.journal.cache_plan({"plan_sha256": "manual-" + coid, "session_id": "MANUAL:" + coid,
                                 "cycle_id": "MANUAL", "trade_date": trade_date, "phase": "MANUAL",
                                 "orders": [order], "executable": True, "frozen_target": {}})
        intent = self.journal.intent(coid)
        if coid in by_remark:
            if intent is None or intent["state"] in ("SUBMITTING", "UNKNOWN"):
                self.journal.mark(coid, "ACKED", str(by_remark[coid].order_id), "found at broker by remark")
            return {**base, "status": "SUBMITTED", "detail": f"already at broker {by_remark[coid].order_id}"}, 0.
        if intent is not None:
            mapped = {"ACKED": "SUBMITTED", "REJECTED": "REJECTED"}.get(intent["state"], "UNKNOWN")
            return {**base, "status": mapped, "detail": f"journal state {intent['state']}"}, 0.
        if side == "SELL" and quantity > int(account.sellable_positions.get(symbol, 0)):
            return {**base, "status": "FAILED", "detail": "more than the sellable quantity"}, 0.
        cost = _buy_cost(quantity, limit) if side == "BUY" else 0.
        if cost > cash:
            return {**base, "status": "FAILED", "detail": f"needs {cost:.2f}, available {cash:.2f}"}, 0.
        self.journal.mark(coid, "SUBMITTING", None, None)
        outcome = self.gateway.submit_limit(symbol=symbol, side=side, quantity=quantity, limit_price=limit,
                                            remark=coid)
        if outcome.status == "SUBMITTED":
            self.journal.mark(coid, "ACKED", outcome.local_order_id, None)
            return {**base, "status": "SUBMITTED", "detail": f"broker order {outcome.local_order_id}"}, cost
        if outcome.status == "REJECTED":
            self.journal.mark(coid, "REJECTED", None, outcome.detail)
            return {**base, "status": "REJECTED", "detail": outcome.detail}, 0.
        self.journal.mark(coid, "UNKNOWN", None, outcome.detail)
        return {**base, "status": "UNKNOWN", "detail": outcome.detail}, cost

    def upload_spool(self) -> dict:
        """Upload snapshots spooled while the server was down, oldest first; move each when accepted."""
        if not self.spool_dir.exists():
            return {"uploaded": [], "failed": None}
        done_dir = self.spool_dir / "uploaded"
        uploaded = []
        for path in sorted(self.spool_dir.glob("snapshot-*.json"), key=lambda p: json.loads(
                p.read_text(encoding="utf-8"))["taken_at"]):
            try:
                self.server.post_oms_snapshot(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.error("spooled snapshot %s still not accepted: %s", path.name, exc)
                return {"uploaded": uploaded, "failed": path.name}
            done_dir.mkdir(exist_ok=True)
            path.replace(done_dir / path.name)
            uploaded.append(path.name)
        self.flush_events()
        return {"uploaded": uploaded, "failed": None}

    def wait_until(self, moment: time) -> None:
        while self.clock().astimezone(CHINA).time() < moment:
            now = self.clock().astimezone(CHINA)
            target = now.replace(hour=moment.hour, minute=moment.minute, second=moment.second, microsecond=0)
            self.sleep(min(30., max(.5, (target - now).total_seconds())))


def _china_now() -> datetime:
    return datetime.now(CHINA)


def main(argv=None) -> int:
    from live_client.config import HYDRA_LIVE_EXECUTABLE_SYMBOLS, LiveClientConfig
    from live_client.execution_queue import SubmissionLockBusy, account_submission_lock
    from live_client.gateway import XtQMTGateway
    from live_client.http_client import LiveServerClient
    from live_client.oms_journal import OmsJournal

    parser = argparse.ArgumentParser(prog="python -m live_client.oms_agent")
    parser.add_argument("command", choices=["pre", "sell", "buy", "cancel", "eod", "intraday", "upload-spool",
                                            "status"])
    parser.add_argument("--date", required=True, help="trade date YYYYMMDD; must be today for sell/buy")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cfg = LiveClientConfig.from_env()
    cfg.validate_startup()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(cfg.log_dir / "hydra-oms-agent.log", encoding="utf-8")])
    server = LiveServerClient(cfg.server_base_url, cfg.api_key, execution_domain=cfg.execution_domain)
    if args.command == "status":
        print(json.dumps(server.get_oms_status(cfg.account_alias), ensure_ascii=False))
        return 0
    if args.command in ("sell", "buy") and not args.dry_run:
        cfg.require_submission_enabled()
    journal = OmsJournal(cfg.state_db.with_name("oms-agent.db"))
    try:
        with account_submission_lock(cfg.userdata_dir, cfg.expected_account_sha256,
                                     wait_seconds=LOCK_WAIT_SECONDS[args.command]):
            gateway = XtQMTGateway(cfg)
            gateway.connect()
            try:
                agent = OmsAgent(account_alias=cfg.account_alias, gateway=gateway, server=server, journal=journal,
                                 clock=_china_now, symbols=sorted(HYDRA_LIVE_EXECUTABLE_SYMBOLS))
                if args.command == "pre":
                    result = agent.pre(args.date)
                elif args.command in ("sell", "buy"):
                    phase = args.command.upper()
                    if not args.dry_run:
                        agent.wait_until(SUBMIT_AT[phase])
                    result = agent.execute(args.date, phase, dry_run=args.dry_run)
                elif args.command == "cancel":
                    result = agent.cancel_open()
                elif args.command == "upload-spool":
                    result = agent.upload_spool()
                elif args.command == "intraday":
                    result = agent.intraday(args.date)
                else:
                    result = agent.eod()
            finally:
                gateway.close()
    except SubmissionLockBusy:
        if args.command != "intraday":
            raise
        # A cycle step holds the account this minute; the next minute's run picks up the work.
        logger.info("account busy with a cycle step; intraday run skipped")
        result = {"status": "SKIPPED_ACCOUNT_BUSY"}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
