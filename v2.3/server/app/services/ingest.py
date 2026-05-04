"""行情入库服务：MarketDataRequest → Parquet。"""
from __future__ import annotations

import pandas as pd

from app.exceptions import APIError, ErrorCode
from app.schemas.market_data import (
    ETFBar, IndexBar, MarketDataReceived, MarketDataRequest,
    MarketDataResponseData, StockBar,
)
from app.storage.parquet import Category, ParquetStore


class IngestService:
    """接收 client 推送的当日行情，按标的写入 Parquet 仓库。

    schema 转换:
      stocks/etfs:  bool is_suspended → int8 suspendFlag (0/1)
      indexes:      不含 suspendFlag
      所有:         drop symbol（文件名编码），加 trade_date（int32，从 req 顶层）
      所有:         drop turnover_rate（v2.3 设计不存）
    """

    def __init__(self, parquet_store: ParquetStore):
        self.store = parquet_store

    def ingest(self, req: MarketDataRequest) -> MarketDataResponseData:
        if not req.stocks and not req.indexes and not req.etfs:
            raise APIError(
                ErrorCode.EMPTY_DATA,
                f"trade_date={req.trade_date} 三类数组均为空",
            )

        trade_date = int(req.trade_date)

        new_stocks = self._write_each("stocks", trade_date, req.stocks, with_suspend=True)
        new_etfs = self._write_each("etfs", trade_date, req.etfs, with_suspend=True)
        new_indexes = self._write_each("indexes", trade_date, req.indexes, with_suspend=False)

        total_new = new_stocks + new_etfs + new_indexes
        total_input = len(req.stocks) + len(req.etfs) + len(req.indexes)

        if total_input > 0 and total_new == 0:
            raise APIError(
                ErrorCode.DUPLICATE_DATE,
                f"trade_date={req.trade_date} 已全部入库（重复推送）",
            )

        return MarketDataResponseData(
            trade_date=req.trade_date,
            received=MarketDataReceived(
                stocks=new_stocks, indexes=new_indexes, etfs=new_etfs,
            ),
            strategy_triggered=True,
        )

    def _write_each(
        self,
        category: Category,
        trade_date: int,
        bars,
        with_suspend: bool,
    ) -> int:
        total_new = 0
        for bar in bars:
            df = self._bar_to_df(bar, trade_date, with_suspend=with_suspend)
            total_new += self.store.append(category, bar.symbol, df)
        return total_new

    @staticmethod
    def _bar_to_df(bar, trade_date: int, with_suspend: bool) -> pd.DataFrame:
        row = {
            "trade_date": trade_date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "amount": bar.amount,
        }
        if with_suspend:
            row["suspendFlag"] = 1 if getattr(bar, "is_suspended", False) else 0
        return pd.DataFrame([row])
