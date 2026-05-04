"""Context：注入给策略的运行时上下文。"""
from __future__ import annotations

import pandas as pd

from app.storage.parquet import Category, ParquetStore


class Context:
    def __init__(
        self,
        instance_id: str,
        trade_date: int,
        virtual_cash: float,
        virtual_positions: dict[str, int],
        parquet_store: ParquetStore,
    ):
        self.instance_id = instance_id
        self.trade_date = trade_date
        self._cash = float(virtual_cash)
        self._positions = dict(virtual_positions)
        self._store = parquet_store

    def cash(self) -> float:
        return self._cash

    def position(self, symbol: str) -> int:
        return int(self._positions.get(symbol, 0))

    def positions(self) -> dict[str, int]:
        return dict(self._positions)

    def market(
        self,
        symbol: str,
        *,
        start_date: int | None = None,
        end_date: int | None = None,
        fields: list[str] | None = None,
        category: Category = "stocks",
    ) -> pd.DataFrame:
        end = self.trade_date if end_date is None else end_date
        df = self._store.read(category, symbol, start_date=start_date, end_date=end)
        if fields and not df.empty:
            keep = ["trade_date"] + [f for f in fields if f in df.columns]
            df = df[keep]
        return df

    def universe(self, category: Category = "stocks") -> list[str]:
        return self._store.list_symbols(category)
