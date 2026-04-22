"""CLI：python -m src.market_data_push --date YYYYMMDD --config path"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from src.common.config import Config, load_config
from src.common.http_client import new_http_client
from src.common.logging_setup import setup_logging
from src.common.notify import notify_wecom
from src.market_data_push.payload import build_market_data_payload
from src.market_data_push.pusher import push_market_data


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.market_data_push",
        description="读取 parquet 并推送到服务器 /market-data。",
    )
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    p.add_argument("--config", required=True, help="settings.yaml 路径")
    return p


def _new_server_client(cfg: Config):
    """可被测试 monkeypatch 替换以注入 MockTransport。"""
    return new_http_client(
        cfg.server.base_url, cfg.server.api_key, cfg.server.timeout,
    )


def _notify(webhook: str, message: str, level: str) -> bool:
    """可被测试 monkeypatch 替换。"""
    return notify_wecom(webhook, message, level)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    logger = setup_logging(cfg.paths.log_dir, "market_data_push")
    logger.info("开始推送 trade_date=%s", args.date)

    parquet_path = Path(cfg.paths.data_root) / "market_data" / f"{args.date}.parquet"
    try:
        payload = build_market_data_payload(parquet_path, trade_date=args.date)
    except (FileNotFoundError, ValueError) as e:
        logger.error("parquet 校验失败: %s", e)
        _notify(cfg.notify.wecom_webhook,
                f"行情推送前校验失败 ({args.date}): {e}", level="alert")
        return 2

    with _new_server_client(cfg) as client:
        result = push_market_data(payload, client, max_retries=3, backoff=2)

    if result.ok:
        received = (result.response_data or {}).get("received_count")
        logger.info("推送完成：received_count=%s", received)
        _notify(cfg.notify.wecom_webhook,
                f"行情推送成功 ({args.date})：{received} 条", level="info")
        return 0

    logger.error("推送最终失败：%s", result.error)
    _notify(cfg.notify.wecom_webhook,
            f"行情推送失败 ({args.date})：{result.error}（已重试 {result.attempts} 次）",
            level="alert")
    return 3


if __name__ == "__main__":
    sys.exit(main())
