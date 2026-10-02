"""Holiday checks against a real QMT before the first OMS session. Never trades.

    python -m live_client.oms_holiday_check --output C:\\private\\hydra-october\\holiday-check-live.json
    python -m live_client.oms_holiday_check --output ...sim.json --reject-probe --confirm-simulation-account <id>

Read-only part (any account): account, positions, today's orders/trades, quotes, and
whether the order query returns anything older than today.

``--reject-probe`` (simulation account only, explicit confirmation of its account id):
submits one 100-share buy of 510300.SH at the 0.001 tick, far outside any price limit,
so the exchange or QMT must refuse it even if the market were open. It records what
order_stock returned, the stored remark (to learn QMT's remark length limit) and the
order status, then cancels the order if QMT somehow accepted it.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

CHINA = timezone(timedelta(hours=8))
PROBE_SYMBOL = "510300.SH"
PROBE_PRICE = 0.001
PROBE_REMARK = "HPROBE-REMARK-LENGTH-0123456789-ABCDEFGHIJ"   # 42 chars: reveals truncation


def readonly_report(gateway, symbols) -> dict:
    now = datetime.now(CHINA)
    account = gateway.account_snapshot()
    orders = gateway.day_orders()
    older = [o for o in orders if o.order_time and
             datetime.fromtimestamp(int(o.order_time), CHINA).strftime("%Y%m%d") < now.strftime("%Y%m%d")]
    trades = gateway.day_trades()
    return {
        "taken_at": now.isoformat(),
        "account_id_suffix": str(account.account_id)[-4:],
        "available_cash": account.available_cash, "total_asset": account.total_asset,
        "positions": account.positions, "sellable": account.sellable_positions,
        "orders_today": len(orders), "orders_older_than_today": len(older),
        "trades_query": "TIMEOUT" if trades is None else len(trades),
        "quotes": gateway.quotes(sorted(symbols)),
    }


def reject_probe(gateway, sleep=time.sleep) -> dict:
    outcome = gateway.submit_limit(symbol=PROBE_SYMBOL, side="BUY", quantity=100, limit_price=PROBE_PRICE,
                                   remark=PROBE_REMARK)
    sleep(3)
    rows = [o for o in gateway.day_orders() if (o.order_remark or "").startswith("HPROBE")]
    cancelled = []
    for row in rows:
        if int(row.order_status) in (48, 49, 50, 55):
            gateway.cancel_order(int(row.order_id))
            cancelled.append(str(row.order_id))
    return {
        "submit_status": outcome.status, "submit_detail": outcome.detail, "local_order_id": outcome.local_order_id,
        "visible_orders": [{"order_id": str(r.order_id), "status": int(r.order_status), "status_msg": r.status_msg,
                            "stored_remark": r.order_remark, "stored_remark_length": len(r.order_remark or "")}
                           for r in rows],
        "sent_remark_length": len(PROBE_REMARK),
        "cancelled": cancelled,
    }


def main(argv=None) -> int:
    from live_client.config import HYDRA_LIVE_EXECUTABLE_SYMBOLS, LiveClientConfig
    from live_client.gateway import XtQMTGateway

    parser = argparse.ArgumentParser(prog="python -m live_client.oms_holiday_check")
    parser.add_argument("--output", required=True)
    parser.add_argument("--reject-probe", action="store_true")
    parser.add_argument("--confirm-simulation-account", default=None)
    args = parser.parse_args(argv)
    cfg = LiveClientConfig.from_env()
    cfg.validate_startup()
    if args.reject_probe and args.confirm_simulation_account != cfg.account_id:
        raise SystemExit("--reject-probe needs --confirm-simulation-account equal to the configured account id; "
                         "never run it against the live account")
    gateway = XtQMTGateway(cfg)
    gateway.connect()
    try:
        report = {"readonly": readonly_report(gateway, HYDRA_LIVE_EXECUTABLE_SYMBOLS)}
        if args.reject_probe:
            report["reject_probe"] = reject_probe(gateway)
    finally:
        gateway.close()
    path = Path(args.output)
    if path.exists():
        raise SystemExit(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"written": str(path), "probe": "reject_probe" in report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
