"""Staging rehearsal: a real HTTP server on a copy of the production DB, the real agent on a
simulated QMT fed with real September bars. Never touches the production service or a broker.

    QMT_DB_URL=sqlite:///<stage>/staging.db PYTHONPATH=<release>/v2.3:<release>/v2.3/server \\
      python scripts/oms_staging_rehearsal.py --base-url http://127.0.0.1:18001 --api-key-file <file> \\
      --stage <stage> --signal-date 20260917 [--restart-unit qmt-oms-staging]

Publishes a cycle from the live instance's current target weights through scripts.oms_ops,
approves it, runs every session, optionally restarts the server between a sell and its EOD,
then checks ledger == simulated broker, one broker order per remark, and the legacy guards.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

SYMBOLS = ["510300.SH", "159915.SZ", "511260.SH", "518880.SH", "159981.SZ", "159985.SZ", "159930.SZ",
           "513500.SH", "513100.SH"]


class HttpServer:
    """LiveServerClient surface over urllib (the staging host has no requests package)."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url, self.headers = base_url.rstrip("/"), {"Authorization": f"Bearer {api_key}"}

    def _call(self, method, path, body=None, params=None):
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={**self.headers, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LookupError(path) from exc
            raise RuntimeError(f"{method} {path} -> {exc.code} {exc.read()[:300]!r}") from exc
        if payload.get("code") != 0:
            raise RuntimeError(f"{method} {path} failed: {payload}")
        return payload["data"]

    def status_code(self, method, path, body=None, params=None, headers=None):
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={**(headers or self.headers), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except (urllib.error.URLError, ConnectionError):
            return 0                                  # not listening yet (e.g. during a restart)

    def post_oms_snapshot(self, payload):
        return self._call("POST", "/oms/live/snapshot", payload)

    def get_oms_plan(self, alias, trade_date, phase):
        return self._call("GET", "/oms/live/plan", params={"account_alias": alias, "trade_date": trade_date,
                                                            "phase": phase})

    def post_oms_events(self, alias, events):
        return self._call("POST", "/oms/live/events", {"account_alias": alias, "events": events})

    def status(self, alias):
        return self._call("GET", "/oms/live/status", params={"account_alias": alias})

    get_oms_status = status

    def get_oms_manual_pending(self, alias, trade_date):
        return self._call("GET", "/oms/live/manual/pending", params={"account_alias": alias, "trade_date": trade_date})

    def post_oms_manual_ack(self, alias, results):
        return self._call("POST", "/oms/live/manual/ack", {"account_alias": alias, "results": results})


def _bars(market_dir: Path, start: str, end: str) -> dict:
    bars = {}
    for symbol in SYMBOLS:
        path = market_dir / f"{symbol}.parquet"
        if not path.exists():
            continue
        frame = pd.read_parquet(path)
        frame = frame[(frame.trade_date >= int(start)) & (frame.trade_date <= int(end))]
        for row in frame.itertuples(index=False):
            bars[(str(row.trade_date), symbol)] = {"open": float(row.open), "high": float(row.high),
                                                   "low": float(row.low), "close": float(row.close),
                                                   "volume": float(row.volume),
                                                   "suspendFlag": int(getattr(row, "suspendFlag", 0) or 0)}
    return bars


def _wait_healthy(server: HttpServer, seconds: int = 60) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if server.status_code("GET", "/healthz") == 200:
            return
        time.sleep(1)
    raise RuntimeError("staging server did not come back")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--signal-date", default="20260917")
    parser.add_argument("--instance", default="live_hydra_v481_rb")
    parser.add_argument("--account", default="hydra-live")
    parser.add_argument("--market-dir", default="/opt/qmt-server/v2.3/server/data/market/daily/etfs")
    parser.add_argument("--restart-unit", default=None)
    args = parser.parse_args()

    from sqlalchemy import select
    from app.db import make_engine, make_session_factory
    from app.models import HydraTarget, InstanceState
    from app.oms.models import OmsCycle
    from app.oms.planner import session_schedule
    from app.settings import get_settings
    from live_client.oms_agent import OmsAgent
    from live_client.oms_journal import OmsJournal
    from live_client.sim_exchange import SimExchange, SimQMTGateway
    from scripts import oms_ops

    stage = Path(args.stage)
    server = HttpServer(args.base_url, Path(args.api_key_file).read_text().strip())
    _wait_healthy(server)
    settings = get_settings()
    sf = make_session_factory(make_engine(settings.db_url))
    with sf() as s:
        inst = s.get(InstanceState, args.instance)
        positions = {k: int(v) for k, v in (inst.virtual_positions or {}).items() if int(v)}
        cash = float(inst.virtual_cash)
        target = s.execute(select(HydraTarget).where(HydraTarget.account_alias == args.account)
                           .order_by(HydraTarget.created_at.desc())).scalars().first()
        raw = target.weights
    weights = ({w["code"]: float(w["weight"]) for w in raw} if isinstance(raw, list)
               else {k: float(v) for k, v in raw.items()})
    bars = _bars(Path(args.market_dir), "20260901", "20260930")
    calendar = sorted({d for d, _ in bars})
    closes = {s: bars[(args.signal_date, s)]["close"] for s in SYMBOLS if (args.signal_date, s) in bars}
    schedule = session_schedule(calendar, args.signal_date, 3)
    files = {}
    for name, value in (("weights", weights), ("closes", closes), ("calendar", calendar)):
        files[name] = stage / f"{name}.json"
        files[name].write_text(json.dumps(value))
    publish = ["publish", "--instance", args.instance, "--account", args.account, "--signal-date", args.signal_date,
               "--weights", str(files["weights"]), "--closes", str(files["closes"]),
               "--calendar", str(files["calendar"]), "--source-sha256", "staging-rehearsal".ljust(64, "0"),
               "--now", f"{args.signal_date[:4]}-{args.signal_date[4:6]}-{args.signal_date[6:]}T20:00:00+08:00"]
    oms_ops.main(publish)                                    # dry run prints the share list
    oms_ops.main(publish + ["--apply"])
    with sf() as s:
        cycle_id = s.execute(select(OmsCycle.cycle_id).where(OmsCycle.status == "PENDING_APPROVAL")).scalar_one()
    oms_ops.main(["approve", "--cycle-id", cycle_id, "--approver", "staging-rehearsal", "--yes"])

    exchange = SimExchange(bars, cash=cash, positions=positions)
    exchange.set_clock(args.signal_date, "160000")
    gateway = SimQMTGateway(exchange)
    gateway.connect()

    def sleep(seconds):
        later = exchange.now() + timedelta(seconds=seconds)
        exchange.set_clock(later.strftime("%Y%m%d"), later.strftime("%H%M%S"))

    agent = OmsAgent(account_alias=args.account, gateway=gateway, server=server,
                     journal=OmsJournal(stage / "agent" / "oms-agent.db", clock=exchange.now), clock=exchange.now,
                     symbols=SYMBOLS, sleep=sleep, spool_dir=stage / "agent" / "spool")
    (stage / "agent").mkdir(exist_ok=True)
    log, trades, all_orders, restarted = [], [], [], False
    for day in [d for d in calendar if args.signal_date < d <= schedule[-1].trade_date]:
        exchange.set_clock(day, "090000")
        log.append({day: {"pre": agent.pre(day)["snapshot"].get("reconciliation")}})
        exchange.set_clock(day, "091505")
        log.append({day: {"buy": agent.execute(day, "BUY")}})
        exchange.set_clock(day, "144500")
        agent.pre(day)
        exchange.set_clock(day, "145500")
        log.append({day: {"cancel": agent.cancel_open()}})
        exchange.set_clock(day, "145705")
        log.append({day: {"sell": agent.execute(day, "SELL")}})
        if args.restart_unit and not restarted and day == schedule[0].trade_date:
            subprocess.run(["systemctl", "restart", args.restart_unit], check=True)
            _wait_healthy(server)
            restarted = True
        exchange.set_clock(day, "150500")
        log.append({day: {"eod": agent.eod()}})
        trades += [dict(t, date=day) for t in exchange.trades()]
        all_orders += [dict(o, date=day) for o in exchange.orders()]

    status = server.status(args.account)
    with sf() as s:
        inst = s.get(InstanceState, args.instance)
        ledger_positions = {k: int(v) for k, v in (inst.virtual_positions or {}).items() if int(v)}
        ledger_cash = float(inst.virtual_cash)
    broker_positions = {s: p["volume"] for s, p in exchange.positions().items() if p["volume"]}
    broker_cash = exchange.asset()["cash"] + exchange.asset()["frozen_cash"]
    remarks = {}
    for order in all_orders:
        remarks[order["order_remark"]] = remarks.get(order["order_remark"], 0) + 1
    checks = {
        "cycle_status": status["cycle"]["status"],
        "ledger_equals_broker_positions": ledger_positions == broker_positions,
        "cash_abs_diff": abs(ledger_cash - broker_cash),
        "one_broker_order_per_remark": all(v == 1 for v in remarks.values()),
        "legacy_orders_refused_409": server.status_code("GET", "/orders", params={"date": calendar[-1]}) == 409,
        "server_restarted_mid_cycle": restarted,
        "fills": len(trades),
    }
    passed = (checks["cycle_status"] == "CLOSED" and checks["ledger_equals_broker_positions"]
              and checks["cash_abs_diff"] < 0.01 and checks["one_broker_order_per_remark"]
              and checks["legacy_orders_refused_409"])
    report = {"verdict": "PASS" if passed else "FAIL", "checks": checks, "schedule": [x.__dict__ for x in schedule],
              "status": status, "trades": trades, "log": log}
    (stage / "rehearsal_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({"verdict": report["verdict"], **checks}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
