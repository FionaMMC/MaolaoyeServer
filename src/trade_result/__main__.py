"""CLI：python -m src.trade_result --stage auction|close --today YYYYMMDD --config path"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from src.auction_order.trader_connector import connect_trader
from src.common.config import Config, load_config
from src.common.db import get_connection, init_schema
from src.common.http_client import new_http_client
from src.common.logging_setup import setup_logging
from src.common.notify import notify_wecom
from src.market_data_download.connector import init_xtquant
from src.trade_result.matcher import match_trades
from src.trade_result.reporter import report_trade_result
from src.trade_result.store import mark_reported, save_trades
from src.trade_result.trades_reader import fetch_trades


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.trade_result",
        description="查询 QMT 成交、写 trades、POST /trade-result。",
    )
    p.add_argument("--stage", required=True, choices=["auction", "close"])
    p.add_argument("--today", help="YYYYMMDD；缺省取本机当天")
    p.add_argument("--config", required=True, help="settings.yaml 路径")
    return p


def _new_server_client(cfg: Config):
    return new_http_client(
        cfg.server.base_url, cfg.server.api_key, cfg.server.timeout,
    )


def _notify(webhook: str, message: str, level: str) -> bool:
    return notify_wecom(webhook, message, level)


def _load_orders(conn: sqlite3.Connection, trade_date: str) -> list[dict]:
    cur = conn.execute(
        """
        SELECT o.order_id, o.signal_id, o.symbol, o.direction,
               o.submitted_quantity
        FROM orders o
        JOIN signals s ON s.signal_id = o.signal_id
        WHERE s.valid_date = ? AND o.submit_status = 'SUCCESS'
        ORDER BY o.submitted_at
        """,
        (trade_date,),
    )
    return [dict(r) for r in cur.fetchall()]


def _summary(records: list) -> str:
    filled = sum(1 for r in records if r.status == "FILLED")
    partial = sum(1 for r in records if r.status == "PARTIAL")
    others = len(records) - filled - partial
    return f"成交 {filled} 单 / 部分 {partial} 单 / 未成 {others} 单"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    logger = setup_logging(cfg.paths.log_dir, "trade_result")
    today = args.today or datetime.now().strftime("%Y%m%d")
    logger.info("开始 %s stage 成交回报，today=%s", args.stage, today)

    init_xtquant(cfg.qmt.data_dir)
    try:
        trader, account = connect_trader(
            data_dir=cfg.qmt.data_dir, account_id=cfg.qmt.account_id,
        )
    except (ValueError, RuntimeError) as e:
        logger.error("QMT 连接失败: %s", e)
        _notify(cfg.notify.wecom_webhook,
                f"{args.stage} 回报脚本启动失败：QMT 连接异常 — {e}", "alert")
        return 2

    qmt_trades = fetch_trades(trader, account)

    db_path = Path(cfg.paths.sqlite_path)
    conn = get_connection(db_path)
    try:
        init_schema(conn)
        orders = _load_orders(conn, today)
        if not orders:
            logger.info("当日 orders 表无记录，无需推送")
            return 0

        records = match_trades(qmt_trades, orders)
        save_trades(conn, records, stage=args.stage)
    finally:
        conn.close()

    with _new_server_client(cfg) as client:
        result = report_trade_result(
            trade_date=today, stage=args.stage,
            records=records, http_client=client,
            max_retries=3, backoff=2,
        )

    conn = get_connection(db_path)
    try:
        if result.ok:
            mark_reported(conn,
                          order_ids=[r.order_id for r in records],
                          report_status="SUCCESS")
        else:
            mark_reported(conn,
                          order_ids=[r.order_id for r in records],
                          report_status="FAILED")
    finally:
        conn.close()

    if not result.ok:
        _notify(cfg.notify.wecom_webhook,
                f"{args.stage} 成交回报推送失败（{today}）：{result.error}", "alert")
        return 3

    label = "竞价成交通知" if args.stage == "auction" else "收盘成交汇总"
    msg = f"{label}（{today}）：{_summary(records)}"
    if result.unmatched_signal_ids:
        _notify(cfg.notify.wecom_webhook,
                f"{args.stage} 存在未匹配信号（{today}）：{result.unmatched_signal_ids}",
                "alert")
        logger.warning("未匹配 signal_ids: %s", result.unmatched_signal_ids)
        return 3

    _notify(cfg.notify.wecom_webhook, msg, "info")
    logger.info("完成 stage=%s: %s", args.stage, _summary(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
