"""CLI：python -m src.auction_order --today YYYYMMDD --config path"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from src.auction_order.signal_reader import read_active_signals
from src.auction_order.store import OrderRecord, save_orders
from src.auction_order.submitter import submit_order
from src.auction_order.trader_connector import connect_trader
from src.common.config import load_config
from src.common.db import get_connection, init_schema
from src.common.logging_setup import setup_logging
from src.common.notify import notify_wecom
from src.market_data_download.connector import init_xtquant


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.auction_order",
        description="09:10 触发：读 signals 表、提交限价单、写 orders 表。",
    )
    p.add_argument("--today", help="YYYYMMDD；缺省取本机当天")
    p.add_argument("--config", required=True, help="settings.yaml 路径")
    return p


def _notify(webhook: str, message: str, level: str) -> bool:
    return notify_wecom(webhook, message, level)


def _fail_record(sig, error: str) -> OrderRecord:
    return OrderRecord(
        order_id=f"fail-{sig.signal_id}",
        signal_id=sig.signal_id,
        symbol=sig.symbol,
        direction=sig.direction,
        submitted_price=0.0,
        submitted_quantity=0,
        submitted_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        submit_status="FAILED",
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    logger = setup_logging(cfg.paths.log_dir, "auction_order")
    today = args.today or datetime.now().strftime("%Y%m%d")
    logger.info("开始竞价下单，today=%s", today)

    init_xtquant(cfg.qmt.data_dir)

    try:
        trader, account = connect_trader(
            data_dir=cfg.qmt.data_dir,
            account_id=cfg.qmt.account_id,
        )
    except (ValueError, RuntimeError) as e:
        logger.error("QMT 连接失败: %s", e)
        _notify(cfg.notify.wecom_webhook,
                f"下单脚本启动失败：QMT 连接异常 — {e}", "alert")
        return 2

    db_path = Path(cfg.paths.sqlite_path)
    conn = get_connection(db_path)
    try:
        init_schema(conn)
        signals = read_active_signals(conn, today=today)
    finally:
        pass

    if not signals:
        logger.info("当日无待下单信号")
        conn.close()
        return 0

    logger.info("待下单 %d 条", len(signals))
    records: list[OrderRecord] = []
    success = 0
    failures: list[tuple[str, str]] = []

    for sig in signals:
        r = submit_order(trader, account, sig)
        if r.ok:
            records.append(OrderRecord(
                order_id=r.order_id or "",
                signal_id=sig.signal_id,
                symbol=sig.symbol,
                direction=sig.direction,
                submitted_price=r.submitted_price or 0.0,
                submitted_quantity=r.submitted_quantity or 0,
                submitted_at=r.submitted_at or "",
                submit_status="SUCCESS",
            ))
            success += 1
        else:
            records.append(_fail_record(sig, r.error or "unknown"))
            failures.append((sig.signal_id, r.error or "unknown"))
            logger.error("signal_id=%s 下单失败: %s", sig.signal_id, r.error)

    save_orders(conn, records)
    conn.close()

    if not failures:
        _notify(cfg.notify.wecom_webhook,
                f"竞价下单完成：{success} 单成功（{today}）", "info")
        return 0

    detail_lines = [f"{sid}: {err}" for sid, err in failures[:10]]
    more = "" if len(failures) <= 10 else f"\n...还有 {len(failures) - 10} 条"
    _notify(cfg.notify.wecom_webhook,
            f"竞价下单完成（{today}）：{success} 单成功 / {len(failures)} 单失败\n"
            + "\n".join(detail_lines) + more,
            "alert")
    return 3


if __name__ == "__main__":
    sys.exit(main())
