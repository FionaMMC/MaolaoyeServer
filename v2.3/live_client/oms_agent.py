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
OUR_REMARK = re.compile(r"^H\d{9}$")
LOT = 100
BUY_COST_FACTOR = 1.001        # matches the planner's size factor (fees and rounding)
MIN_FEE = 5.0
_SIDES = {QMT_STOCK_BUY: "BUY", QMT_STOCK_SELL: "SELL"}


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
    def snapshot(self, kind: str) -> dict:
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
            path = self._spool(payload)
            logger.error("snapshot upload failed (%s); spooled to %s", exc, path)
            result = {"uploaded": False, "spooled": str(path)}
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
                while quantity > 0 and quantity * limit * BUY_COST_FACTOR + MIN_FEE > cash:
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
                    cash -= quantity * limit * BUY_COST_FACTOR + MIN_FEE
            elif outcome.status == "REJECTED":
                self.journal.mark(coid, "REJECTED", None, outcome.detail)
            else:
                self.journal.mark(coid, "UNKNOWN", None, outcome.detail)
            results.append({"client_order_id": coid, "status": outcome.status, "quantity": quantity})
        self.flush_events()
        return {"status": "DRY_RUN" if dry_run else "EXECUTED", "orders": results}

    def poll(self, until: time, interval: float = 30.) -> int:
        count = 0
        while True:
            self.snapshot("ADHOC")
            count += 1
            if self.clock().astimezone(CHINA).time() >= until:
                return count
            self.sleep(interval)

    def cancel_open(self, wait_seconds: float = 60., step: float = 5.) -> dict:
        if self.clock().astimezone(CHINA).time() >= CANCEL_DEADLINE:
            return {"status": "TOO_LATE"}
        requested = []
        for order in self.gateway.day_orders():
            remark = (order.order_remark or "").strip()
            if (OUR_REMARK.match(remark) and _SIDES.get(int(order.order_type or 0)) == "BUY"
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
        return self.snapshot("EOD")

    def wait_until(self, moment: time) -> None:
        while self.clock().astimezone(CHINA).time() < moment:
            now = self.clock().astimezone(CHINA)
            target = now.replace(hour=moment.hour, minute=moment.minute, second=moment.second, microsecond=0)
            self.sleep(min(30., max(.5, (target - now).total_seconds())))


def _china_now() -> datetime:
    return datetime.now(CHINA)


def main(argv=None) -> int:
    from live_client.config import HYDRA_LIVE_EXECUTABLE_SYMBOLS, LiveClientConfig
    from live_client.execution_queue import account_submission_lock
    from live_client.gateway import XtQMTGateway
    from live_client.http_client import LiveServerClient
    from live_client.oms_journal import OmsJournal

    parser = argparse.ArgumentParser(prog="python -m live_client.oms_agent")
    parser.add_argument("command", choices=["pre", "sell", "buy", "cancel", "eod", "status"])
    parser.add_argument("--date", required=True, help="trade date YYYYMMDD; must be today for sell/buy")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--poll-until", default=None, help="HHMM; keep taking ADHOC snapshots until then")
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
    with account_submission_lock(cfg.userdata_dir, cfg.expected_account_sha256):
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
                if args.poll_until:
                    agent.poll(time(int(args.poll_until[:2]), int(args.poll_until[2:])))
            elif args.command == "cancel":
                result = agent.cancel_open()
            else:
                result = agent.eod()
        finally:
            gateway.close()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
