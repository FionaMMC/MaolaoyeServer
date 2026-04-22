"""CLI：python -m src.signal_query --today YYYYMMDD --config path"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from src.common.config import Config, load_config
from src.common.db import get_connection, init_schema
from src.common.http_client import new_http_client
from src.common.logging_setup import setup_logging
from src.common.notify import notify_wecom
from src.market_data_download.connector import init_xtquant
from src.signal_query.fetcher import fetch_signals
from src.signal_query.next_trading_day import next_trading_day
from src.signal_query.store import save_signals
from src.signal_query.validator import validate_signals


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.signal_query",
        description="查询服务器信号、校验、写入 SQLite、通知。",
    )
    p.add_argument("--today", help="YYYYMMDD；缺省取本机当天")
    p.add_argument("--config", required=True, help="settings.yaml 路径")
    p.add_argument("--wait-secs", type=int, default=1800,
                   help="遇到 3002 后等待秒数（默认 1800=30min）")
    return p


def _new_server_client(cfg: Config):
    return new_http_client(
        cfg.server.base_url, cfg.server.api_key, cfg.server.timeout,
    )


def _notify(webhook: str, message: str, level: str) -> bool:
    return notify_wecom(webhook, message, level)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    logger = setup_logging(cfg.paths.log_dir, "signal_query")
    today = args.today or datetime.now().strftime("%Y%m%d")
    logger.info("开始查询信号，today=%s", today)

    init_xtquant(cfg.qmt.data_dir)
    try:
        next_date = next_trading_day(today)
    except (ValueError, RuntimeError) as e:
        logger.error("next_trading_day 失败: %s", e)
        _notify(cfg.notify.wecom_webhook,
                f"信号查询 next_trading_day 失败: {e}", level="alert")
        return 2

    logger.info("next_trade_date=%s", next_date)

    with _new_server_client(cfg) as client:
        result = fetch_signals(next_date, client, wait_secs=args.wait_secs)

    if result.state == "NO_SIGNALS":
        _notify(cfg.notify.wecom_webhook, f"今日无交易信号（{next_date}）", "info")
        return 0

    if result.state == "STILL_PENDING":
        _notify(cfg.notify.wecom_webhook,
                f"信号查询失败 ({next_date})：服务器策略未完成（3002）重试后仍未就绪",
                "alert")
        return 3

    if result.state == "ERROR":
        _notify(cfg.notify.wecom_webhook,
                f"信号查询失败 ({next_date})：{result.error}", "alert")
        return 3

    valid, rejected = validate_signals(result.signals, expected_date=next_date)
    if rejected:
        logger.warning("%d 条信号被拒：%s",
                       len(rejected),
                       [(r.get('signal_id'), r.get('_reason')) for r in rejected])

    if not valid:
        _notify(cfg.notify.wecom_webhook,
                f"信号查询拿到 {len(result.signals)} 条但全部不合法（{next_date}）",
                "alert")
        return 3

    db_path = Path(cfg.paths.sqlite_path)
    conn = get_connection(db_path)
    try:
        init_schema(conn)
        n = save_signals(conn, valid)
    finally:
        conn.close()

    summary_parts = [f"已制单（{next_date}），共 {n} 条信号"]
    if rejected:
        summary_parts.append(f"{len(rejected)} 条被拒")
    _notify(cfg.notify.wecom_webhook, "；".join(summary_parts), "info")
    logger.info("完成。valid=%d rejected=%d", n, len(rejected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
