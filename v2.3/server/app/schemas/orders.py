"""GET /orders 响应 schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class OrderItem(BaseModel):
    order_id: str
    account_group: str
    symbol: str
    direction: str = Field(pattern=r"^(BUY|SELL)$")
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    valid_date: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")


class OrdersResponseData(BaseModel):
    date: str
    orders: list[OrderItem]
