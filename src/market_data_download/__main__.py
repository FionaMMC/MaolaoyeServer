"""CLI：python -m src.market_data_download --date YYYYMMDD --config path"""
from __future__ import annotations

import argparse
import sys

from src.common.config import load_config
from src.common.logging_setup import setup_logging
from src.market_data_download.cleaner import clean_market_data
from src.market_data_download.connector import startup_check
from src.market_data_download.downloader import download_daily_market_data
from src.market_data_download.storage import save_market_data_parquet


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.market_data_download",
        description="下载并清洗当日行情，写入 parquet。",
    )
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    p.add_argument("--config", required=True, help="settings.yaml 路径")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    logger = setup_logging(
        log_dir=cfg.paths.log_dir,
        module_name="market_data_download",
    )
    logger.info("开始执行，trade_date=%s", args.date)

    try:
        startup_check(data_dir=cfg.qmt.data_dir)
        raw = download_daily_market_data(
            trade_date=args.date,
            sector_name=cfg.market_data.sector_name,
        )
    except (RuntimeError, ValueError) as e:
        logger.error("下载阶段失败: %s", e)
        return 2

    try:
        df = clean_market_data(raw)
    except ValueError as e:
        logger.error("清洗后无有效数据: %s", e)
        return 3

    out_path = save_market_data_parquet(
        df, trade_date=args.date, data_root=cfg.paths.data_root,
    )
    logger.info("完成，输出 %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
