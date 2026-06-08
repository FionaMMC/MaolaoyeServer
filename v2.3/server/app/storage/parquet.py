"""Parquet 行情仓库：按 (category, symbol) 一文件，trade_date 主键去重 + 升序。

布局:
  {root}/market/daily/stocks/{symbol}.parquet
  {root}/market/daily/indexes/{symbol}.parquet
  {root}/market/daily/etfs/{symbol}.parquet

写入策略：read-merge-dedupe-write（小文件够用，pyarrow 无原生 append-with-dedup）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import pandas as pd

Category = Literal["stocks", "indexes", "etfs"]
_PK = "trade_date"


class ParquetStore:
    """Parquet 行情仓库。schema 由调用方保证，store 不做字段校验。"""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _file(self, category: Category, symbol: str) -> Path:
        return self.root / "market" / "daily" / category / f"{symbol}.parquet"

    def _category_dir(self, category: Category) -> Path:
        return self.root / "market" / "daily" / category

    def append(self, category: Category, symbol: str, df: pd.DataFrame) -> int:
        """追加写入，按 trade_date 主键去重。返回实际新增行数。

        语义：已存在的 trade_date 保留旧值（先到先得），新 trade_date 追加。
        """
        if df.empty:
            return 0
        if _PK not in df.columns:
            raise ValueError(f"DataFrame 必须含 '{_PK}' 列")

        path = self._file(category, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            existing = pd.read_parquet(path)
            existing_dates = set(existing[_PK])
            new_rows = df[~df[_PK].isin(existing_dates)]
            if new_rows.empty:
                return 0
            merged = pd.concat([existing, new_rows], ignore_index=True)
        else:
            new_rows = df
            merged = df.copy()

        merged.sort_values(_PK, inplace=True, ignore_index=True)
        merged.to_parquet(path, engine="pyarrow", index=False)
        return len(new_rows)

    def read(
        self,
        category: Category,
        symbol: str,
        start_date: int | None = None,
        end_date: int | None = None,
    ) -> pd.DataFrame:
        """读取单标的历史，可选日期范围（含端点）。

        缺失文件返回空 DataFrame（不抛异常）。
        """
        path = self._file(category, symbol)
        if not path.exists():
            return pd.DataFrame()

        df = pd.read_parquet(path)
        if start_date is not None:
            df = df[df[_PK] >= start_date]
        if end_date is not None:
            df = df[df[_PK] <= end_date]
        return df.reset_index(drop=True)

    def has_date(self, category: Category, symbol: str, trade_date: int) -> bool:
        path = self._file(category, symbol)
        if not path.exists():
            return False
        df = pd.read_parquet(path, columns=[_PK])
        return bool((df[_PK] == trade_date).any())

    def latest_date(self, category: Category, symbol: str) -> int | None:
        """返回该标的最新 trade_date；文件缺失或空返回 None。"""
        path = self._file(category, symbol)
        if not path.exists():
            return None
        df = pd.read_parquet(path, columns=[_PK])
        if df.empty:
            return None
        return int(df[_PK].max())

    def list_symbols(self, category: Category) -> list[str]:
        d = self._category_dir(category)
        if not d.exists():
            return []
        return [p.stem for p in d.glob("*.parquet")]
