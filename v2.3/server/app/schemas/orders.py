"""GET /orders 响应 schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.execution import ExecutionDomain


class OrderItem(BaseModel):
    order_id: str
    execution_domain: ExecutionDomain = "paper"
    qmt_account_alias: str | None = None
    target_id: str | None = None
    rebalance_id: str | None = None
    attempt_id: str | None = None
    attempt_number: int | None = None
    batch_id: str | None = None
    batch_sha256: str | None = None
    target_hash: str | None = None
    execution_reference_price: float | None = None
    account_group: str
    symbol: str
    direction: str = Field(pattern=r"^(BUY|SELL)$")
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    valid_date: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")


class OrdersResponseData(BaseModel):
    date: str
    orders: list[OrderItem]
