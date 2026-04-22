"""全市场日线行情下载。v3 历史教训已内化。"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = [
    "open", "high", "low", "close",
    "volume", "amount",
    "turnoverRatio",
    "suspendFlag",
]


def _is_trading_day(trade_date: str) -> bool:
    """交易日判断内联版：xtdata.get_trading_dates。"""
    from xtquant import xtdata

    dates = xtdata.get_trading_dates("SH", count=30)
    as_str = [str(d)[:8] if not isinstance(d, str) else d for d in dates]
    return trade_date in as_str


def download_daily_market_data(
    trade_date: str,
    sector_name: str,
) -> dict[str, Any]:
    """下载指定交易日的全市场日线行情。

    Args:
        trade_date: YYYYMMDD
        sector_name: 板块名字符串，如 "沪深A股"（不是指数代码！）

    Returns:
        {"trade_date", "symbols", "market_data": {field: DataFrame}}

    Raises:
        ValueError: trade_date 非交易日
        RuntimeError: 板块成分为空
    """
    from xtquant import xtdata

    if not _is_trading_day(trade_date):
        raise ValueError(f"{trade_date} 非交易日，不下载")

    logger.info("拉取板块 %s 的成分股", sector_name)
    symbols = xtdata.get_stock_list_in_sector(sector_name)
    if not symbols:
        raise RuntimeError(f"板块 {sector_name} 返回空，请检查板块名是否正确")
    logger.info("板块 %s 共 %d 只股票", sector_name, len(symbols))

    logger.info("开始 download_history_data（period=1d, date=%s）", trade_date)
    for sym in symbols:
        try:
            xtdata.download_history_data(
                sym,
                period="1d",
                start_time=trade_date,
                end_time=trade_date,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("download_history_data 失败 %s: %s", sym, e)

    # v3 历史教训：download 后必须 sleep(1) 再 get_market_data
    time.sleep(1)

    logger.info("读取 get_market_data，字段 %s", _REQUIRED_FIELDS)
    md = xtdata.get_market_data(
        _REQUIRED_FIELDS,
        symbols,
        period="1d",
        start_time=trade_date,
        end_time=trade_date,
    )

    return {
        "trade_date": trade_date,
        "symbols": symbols,
        "market_data": md,
    }
