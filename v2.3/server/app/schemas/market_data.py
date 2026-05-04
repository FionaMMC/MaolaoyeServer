"""POST /market-data 请求/响应 schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class StockBar(BaseModel):
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    is_suspended: bool
    turnover_rate: float | None = None


class IndexBar(BaseModel):
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    amount: float = 0.0


class ETFBar(StockBar):
    """ETF 同 stock schema。"""


class MarketDataRequest(BaseModel):
    trade_date: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")
    stocks: list[StockBar] = Field(default_factory=list)
    indexes: list[IndexBar] = Field(default_factory=list)
    etfs: list[ETFBar] = Field(default_factory=list)


class MarketDataReceived(BaseModel):
    stocks: int
    indexes: int
    etfs: int


class MarketDataResponseData(BaseModel):
    trade_date: str
    received: MarketDataReceived
    strategy_triggered: bool = True
