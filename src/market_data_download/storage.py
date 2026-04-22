"""Parquet 持久化。"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def save_market_data_parquet(
    df: pd.DataFrame,
    trade_date: str,
    data_root: Path | str,
) -> Path:
    """保存清洗后的行情到 parquet：{data_root}/market_data/{trade_date}.parquet"""
    data_root = Path(data_root)
    out_dir = data_root / "market_data"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / f"{trade_date}.parquet"
    df.to_parquet(out_path, engine="pyarrow", index=False)
    logger.info("行情写入 %s（%d 行）", out_path, len(df))
    return out_path
