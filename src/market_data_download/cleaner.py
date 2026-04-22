"""将 downloader 返回的原始结构清洗为规范化长格式 DataFrame。"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_OUTPUT_COLUMNS = [
    "symbol", "trade_date", "open", "high", "low", "close",
    "volume", "amount", "turnover_rate", "is_suspended",
]


def clean_market_data(raw: dict[str, Any]) -> pd.DataFrame:
    """清洗合并 downloader 的返回。

    Returns: 10 列长格式 DataFrame。
    """
    trade_date = raw["trade_date"]
    symbols = raw["symbols"]
    md = raw["market_data"]

    if not symbols:
        raise ValueError("market_data 为空，无数据可清洗")

    def _col(field: str) -> pd.Series:
        df = md.get(field, pd.DataFrame())
        if df.empty or trade_date not in df.columns:
            return pd.Series(dtype="float64", index=symbols)
        return df[trade_date]

    df = pd.DataFrame({
        "symbol": symbols,
        "trade_date": trade_date,
        "open": _col("open").reindex(symbols).astype("float64").values,
        "high": _col("high").reindex(symbols).astype("float64").values,
        "low": _col("low").reindex(symbols).astype("float64").values,
        "close": _col("close").reindex(symbols).astype("float64").values,
        "volume": _col("volume").reindex(symbols).fillna(0).astype("int64").values,
        "amount": _col("amount").reindex(symbols).astype("float64").values,
        "turnover_rate": _col("turnoverRatio").reindex(symbols).astype("float64").values,
        "is_suspended": (
            _col("suspendFlag").reindex(symbols).fillna(0).astype("int64") == 1
        ).values,
    })

    before = len(df)
    ohlcv_zero = (
        (df["open"] == 0) & (df["high"] == 0) & (df["low"] == 0) & (df["close"] == 0)
    )
    df = df.loc[~(ohlcv_zero & ~df["is_suspended"])].reset_index(drop=True)
    dropped = before - len(df)
    if dropped:
        logger.info("丢弃 %d 条非停牌 0 值行", dropped)

    df = df[_OUTPUT_COLUMNS]
    logger.info("清洗完成：%d 行 × %d 列", len(df), len(df.columns))
    return df
