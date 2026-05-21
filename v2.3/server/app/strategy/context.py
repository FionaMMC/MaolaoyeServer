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
        risk_blacklist: set[str] | None = None,
        strategy_state: dict | None = None,
    ):
        self.instance_id = instance_id
        self.trade_date = trade_date
        self._cash = float(virtual_cash)
        self._positions = dict(virtual_positions)
        self._store = parquet_store
        self._risk_blacklist = set(risk_blacklist or ())
        # 策略持久状态：strategy 通过 strategy_state() 读，run() 完返回的新状态
        # 通过 _next_strategy_state 写。pipeline 跑完会把 _next_strategy_state
        # 落库到 InstanceState.strategy_state。
        self._strategy_state = dict(strategy_state) if strategy_state else {}
        self._next_strategy_state: dict | None = None

    def cash(self) -> float:
        return self._cash

    def position(self, symbol: str) -> int:
        return int(self._positions.get(symbol, 0))

    def positions(self) -> dict[str, int]:
        return dict(self._positions)

    def risk_blacklist(self) -> set[str]:
        """QMT 历史拒单的 symbol 集合（ST/退市/协议未签 等），策略不应再下单。"""
        return set(self._risk_blacklist)

    def is_blacklisted(self, symbol: str) -> bool:
        return symbol in self._risk_blacklist

    def strategy_state(self) -> dict:
        """读上一次落盘的策略状态（last_rb_idx 等）。首次运行返回 {}。"""
        return dict(self._strategy_state)

    def set_strategy_state(self, state: dict) -> None:
        """run() 收尾时调用：把本次产出的新状态交给 pipeline 落库。"""
        if state is None:
            self._next_strategy_state = None
        else:
            self._next_strategy_state = dict(state)

    def pop_next_strategy_state(self) -> dict | None:
        """pipeline 内部：取本次 run 写下的状态，None 表示策略没调 set。"""
        return self._next_strategy_state

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
