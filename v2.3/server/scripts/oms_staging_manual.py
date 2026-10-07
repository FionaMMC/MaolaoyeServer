"""Staging rehearsal of the dashboard operator flows over real HTTP (DB copy, simulated QMT).

    QMT_DB_URL=sqlite:///<stage>/staging.db PYTHONPATH=<release>/v2.3:<release>/v2.3/server \\
      python scripts/oms_staging_manual.py --base-url http://127.0.0.1:18001 --stage <stage> \\
      --live-key-file <file> --operator-key-file <file>

Today's bars are borrowed from the latest real session (the simulator only needs prices),
so a manual buy can fill and reach the strategy ledger through the normal EOD snapshot.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

CHINA = timezone(timedelta(hours=8))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--live-key-file", required=True)
    parser.add_argument("--operator-key-file", required=True)
    parser.add_argument("--account", default="hydra-live")
    parser.add_argument("--market-dir", default="/opt/qmt-server/v2.3/server/data/market/daily/etfs")
    parser.add_argument("--borrow-date", default="20260930")
    args = parser.parse_args()

    from scripts.oms_staging_rehearsal import SYMBOLS, HttpServer, _bars, _wait_healthy
    from live_client.oms_agent import OmsAgent
    from live_client.oms_journal import OmsJournal
    from live_client.sim_exchange import SimExchange, SimQMTGateway

    stage = Path(args.stage)
    agent_api = HttpServer(args.base_url, Path(args.live_key_file).read_text().strip())
    operator = HttpServer(args.base_url, Path(args.operator_key_file).read_text().strip())
    _wait_healthy(agent_api)
    today = datetime.now(CHINA).strftime("%Y%m%d")
    checks = {"page_served": agent_api.status_code("GET", "/dashboard/oms", headers={}) == 200}

    before = operator._call("GET", "/oms/live/overview", params={"account_alias": args.account})
    positions, cash = before["ledger"]["positions"], before["ledger"]["cash"]
    borrowed = _bars(Path(args.market_dir), args.borrow_date, args.borrow_date)
    bars = {(today, s): b for (d, s), b in borrowed.items()}
    close = bars[(today, "510300.SH")]["close"]
    limit = round(close * 1.01, 3)

    order = operator._call("POST", "/oms/live/manual/orders", {
        "account_alias": args.account, "symbol": "510300.SH", "side": "BUY", "quantity": 100, "limit_price": limit,
        "reason": "staging rehearsal manual buy", "operator": "staging", "confirm": True})
    cancel = operator._call("POST", "/oms/live/manual/cancels", {
        "account_alias": args.account, "broker_order_id": "999999999", "reason": "staging rehearsal cancel",
        "operator": "staging", "confirm": True})
    held = positions.get("511260.SH") or 100
    dividend_body = {"account_alias": args.account, "symbol": "511260.SH", "record_date": "20261015",
                     "ex_date": "20261016", "pay_date": "20261020", "entitled_quantity": held, "cash_per_share": 0.35,
                     "evidence": "staging rehearsal dividend evidence", "operator": "staging",
                     "reason": "staging rehearsal dividend"}
    preview = operator._call("POST", "/oms/live/dividends/preview", dividend_body)
    registered = operator._call("POST", "/oms/live/dividends", dividend_body)

    checks["operator_cannot_reach_agent_api"] = operator.status_code(
        "GET", "/oms/live/manual/pending", params={"account_alias": args.account}) == 403
    checks["agent_cannot_issue_manual_orders"] = agent_api.status_code(
        "POST", "/oms/live/manual/orders", body={**dividend_body}) == 403

    exchange = SimExchange(bars, cash=cash, positions={k: int(v) for k, v in positions.items()})
    exchange.set_clock(today, "100000")
    gateway = SimQMTGateway(exchange)
    gateway.connect()
    (stage / "manual-agent").mkdir(exist_ok=True)
    agent = OmsAgent(account_alias=args.account, gateway=gateway, server=agent_api,
                     journal=OmsJournal(stage / "manual-agent" / "oms-agent.db", clock=exchange.now),
                     clock=exchange.now, symbols=SYMBOLS, spool_dir=stage / "manual-agent" / "spool")
    manual_run = agent.intraday(today)                 # the every-minute task: instructions, then a snapshot
    second_run = agent.intraday(today)
    exchange.set_clock(today, "150500")
    eod = agent.eod()
    after = operator._call("GET", "/oms/live/overview", params={"account_alias": args.account})

    manual = {o["client_order_id"]: o for o in after["manual_orders"]}
    instructions = {i["instruction_id"]: i for i in after["instructions"]}
    checks.update({
        "manual_order_filled": manual[order["client_order_id"]]["state"] == "FILLED",
        "ledger_position_increased_by_100":
            after["ledger"]["positions"].get("510300.SH", 0) == positions.get("510300.SH", 0) + 100,
        "order_instruction_done": instructions[order["instruction_id"]]["status"] == "DONE",
        "cancel_of_unknown_order_failed": instructions[cancel["instruction_id"]]["status"] == "FAILED",
        "second_run_found_nothing": second_run["manual"]["status"] == "NOTHING_PENDING",
        "intraday_snapshots_uploaded": all("snapshot_id" in (run["snapshot"] or {}) for run in (manual_run, second_run)),
        "dividend_preview_amount": preview["amount"],
        "dividend_registered": registered["applied"] is True and len(after["dividends"]) >= 1,
        "audit_actions": sorted({o["action"] for o in after["overrides"]}),
        "eod_reconciled": eod["reconciliation"]["passed"],
        "one_broker_order_for_the_manual_remark":
            sum(1 for o in exchange.orders() if o["order_remark"] == order["client_order_id"]) == 1,
    })
    passed = all(v for k, v in checks.items() if isinstance(v, bool))
    report = {"verdict": "PASS" if passed else "FAIL", "checks": checks, "order": order, "cancel": cancel,
              "manual_run": manual_run, "eod": eod, "overview_after": after}
    (stage / "manual_rehearsal_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({"verdict": report["verdict"], **checks}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
