"""POST /trade-result 请求/响应 schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TradeResult(BaseModel):
    order_id: str
    filled_quantity: int = Field(ge=0)
    filled_price: float = Field(ge=0)
    filled_time: str | None = None
    status: str = Field(pattern=r"^(FILLED|PARTIAL|CANCELLED|REJECTED)$")
    # 可选（新客户端携带）：order_id 未匹配时用于定位候选订单。
    symbol: str | None = None
    direction: str | None = Field(default=None, pattern=r"^(BUY|SELL)$")


class TradeResultRequest(BaseModel):
    trade_date: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")
    results: list[TradeResult]


class TradeResultResponseData(BaseModel):
    trade_date: str
    matched_count: int
    unmatched_order_ids: list[str] = Field(default_factory=list)
    # unmatched order_id → 当日库内候选订单（symbol/direction/quantity 相符）。
    # 场景：管线重算换掉 order_id 后客户端按旧 ID 回报（2026-07-02 事故），
    # 候选让人工恢复从『猜』变成『核对后确认』。
    unmatched_candidates: dict[str, list[str]] = Field(default_factory=dict)
