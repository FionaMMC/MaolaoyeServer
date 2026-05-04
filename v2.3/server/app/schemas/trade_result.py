"""POST /trade-result 请求/响应 schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TradeResult(BaseModel):
    order_id: str
    filled_quantity: int = Field(ge=0)
    filled_price: float = Field(ge=0)
    filled_time: str | None = None
    status: str = Field(pattern=r"^(FILLED|PARTIAL|CANCELLED|REJECTED)$")


class TradeResultRequest(BaseModel):
    trade_date: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")
    results: list[TradeResult]


class TradeResultResponseData(BaseModel):
    trade_date: str
    matched_count: int
    unmatched_order_ids: list[str] = Field(default_factory=list)
