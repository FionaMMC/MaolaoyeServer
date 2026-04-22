"""将模块一输出的 parquet 构造为 POST /market-data JSON。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def build_market_data_payload(
    parquet_path: Path | str,
    trade_date: str,
) -> dict[str, Any]:
    """读取 parquet，构造符合 POST /market-data 的 JSON 字典。

    Raises:
        FileNotFoundError: parquet 不存在
        ValueError: trade_date 与 parquet 内容不一致 或 parquet 为空
    """
    parquet_path = Path(parquet_path)
    if not parquet_path.exists():
        raise FileNotFoundError(f"parquet 不存在: {parquet_path}")

    df = pd.read_parquet(parquet_path)
    if df.empty:
        raise ValueError("parquet 空，无数据可推送")

    # 校验所有行的 trade_date 一致且等于期望值
    dates = df["trade_date"].unique()
    if set(dates) != {trade_date}:
        raise ValueError(
            f"parquet 的 trade_date 与期望不一致：期望 {trade_date}，实际 {sorted(dates)}"
        )

    stocks: list[dict[str, Any]] = []
    for row in df.itertuples(index=False):
        stocks.append({
            "symbol": str(row.symbol),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": int(row.volume),
            "amount": float(row.amount),
            "turnover_rate": float(row.turnover_rate),
            "is_suspended": bool(row.is_suspended),
        })

    logger.info("构造 payload: %d 条 stocks，trade_date=%s", len(stocks), trade_date)
    return {"trade_date": trade_date, "stocks": stocks}
