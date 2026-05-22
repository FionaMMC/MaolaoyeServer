"""
模块：行情下载并写入本地 parquet（C:\parttime\strategy search\data\market\daily）

用法:
    python market_push_local.py                    # 默认当天
    python market_push_local.py --date 20260522   # 指定日期
    python market_push_local.py --date 20260522 --full  # 全量写入（覆盖历史）
"""

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from xtquant import xtdata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

xtdata.data_dir = config.QMT_USERDATA_DIR
log = config.setup_logger("market_push_local")

DATA_BASE = Path(r"C:\parttime\strategy search\data\market\daily")
STOCKS_DIR = DATA_BASE / "stocks"
ETFS_DIR = DATA_BASE / "etfs"
INDEXES_DIR = DATA_BASE / "indexes"

COLUMNS = ["trade_date", "open", "high", "low", "close", "volume", "amount", "suspendFlag"]


def startup_check() -> tuple[list, list, list]:
    assert os.path.exists(config.QMT_USERDATA_DIR), \
        f"QMT 数据目录不存在: {config.QMT_USERDATA_DIR}"
    stock_list = xtdata.get_stock_list_in_sector(config.STOCK_SECTOR)
    log.info(f"板块 '{config.STOCK_SECTOR}'：{len(stock_list)} 只股票")
    etf_set = set()
    for sector in config.ETF_SECTORS:
        members = xtdata.get_stock_list_in_sector(sector)
        log.info(f"板块 '{sector}'：{len(members)} 只 ETF")
        etf_set.update(members)
    index_list = list(getattr(config, "INDEX_CODES", []))
    log.info(f"指数：{len(index_list)} 只")
    return stock_list, list(etf_set), index_list


def _check_qmt_alive() -> bool:
    try:
        tick = xtdata.get_full_tick(["000001.SH"])
        return tick is not None and len(tick) > 0
    except Exception as e:
        log.error(f"QMT 连通性检测异常：{e}")
        return False


def _download_all(all_codes: list) -> None:
    log.info(f"下载行情 {len(all_codes)} 个标的...")
    start = time.time()
    done = threading.Event()

    def _heartbeat():
        while not done.wait(30):
            log.info(f"  仍在下载...（{time.time() - start:.0f} 秒）")

    t = threading.Thread(target=_heartbeat, daemon=True)
    t.start()
    try:
        xtdata.download_history_data2(all_codes, period="1d", start_time="", end_time="")
    finally:
        done.set()
    log.info(f"下载完成（{time.time() - start:.0f} 秒）")
    time.sleep(3)


def _read_klines(codes: list, trade_date: str, dividend_type: str) -> dict:
    if not codes:
        return {}
    return xtdata.get_market_data_ex(
        field_list=["open", "high", "low", "close", "volume", "amount", "suspendFlag"],
        stock_list=codes,
        period="1d",
        start_time=trade_date,
        end_time=trade_date,
        count=-1,
        dividend_type=dividend_type,
        fill_data=False,
    ) or {}


def _save_to_parquet(output_dir: Path, raw_data: dict, trade_date: str, full: bool) -> int:
    """将 K 线数据写入 parquet。每个标的单独一个文件，日期不重复则追加。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped_total = 0

    for symbol, df in raw_data.items():
        if df is None or df.empty:
            continue
        row = df.iloc[-1]
        if row.isna().all():
            continue

        fpath = output_dir / f"{symbol}.parquet"
        new_row = {
            "trade_date": int(trade_date),
            "open": float(row.get("open", 0) or 0),
            "high": float(row.get("high", 0) or 0),
            "low": float(row.get("low", 0) or 0),
            "close": float(row.get("close", 0) or 0),
            "volume": int(row.get("volume", 0) or 0),
            "amount": float(row.get("amount", 0) or 0),
            "suspendFlag": int(row.get("suspendFlag", 0) or 0),
        }

        if full or not fpath.exists():
            new_df = pd.DataFrame([new_row], columns=COLUMNS)
            new_df.to_parquet(fpath, index=False)
            written += 1
            continue

        existing = pd.read_parquet(fpath)
        if int(trade_date) in existing["trade_date"].values:
            skipped_total += 1
            continue

        combined = pd.concat([existing, pd.DataFrame([new_row])], ignore_index=True)
        combined.to_parquet(fpath, index=False)
        written += 1

    return written


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="交易日 YYYYMMDD（默认当天）")
    parser.add_argument("--full", action="store_true", help="全量覆盖写入")
    args = parser.parse_args()

    trade_date = args.date or datetime.now().strftime("%Y%m%d")
    full = args.full

    log.info(f"=== market_push_local 启动  trade_date={trade_date}  full={full} ===")
    startup_check()

    stock_list, etf_list, index_list = startup_check()
    all_codes = stock_list + etf_list + index_list

    if not _check_qmt_alive():
        log.error("QMT 连通性检测失败，退出")
        return

    _download_all(all_codes)

    stocks_raw = _read_klines(stock_list, trade_date, "front")
    etfs_raw = _read_klines(etf_list, trade_date, "front")
    indexes_raw = _read_klines(index_list, trade_date, "none")

    n_stocks = _save_to_parquet(STOCKS_DIR, stocks_raw, trade_date, full)
    n_etfs = _save_to_parquet(ETFS_DIR, etfs_raw, trade_date, full)
    n_indexes = _save_to_parquet(INDEXES_DIR, indexes_raw, trade_date, full)

    log.info(f"写入完成：stocks={n_stocks} etfs={n_etfs} indexes={n_indexes}")
    log.info("=== market_push_local 完成 ===")


if __name__ == "__main__":
    main()
