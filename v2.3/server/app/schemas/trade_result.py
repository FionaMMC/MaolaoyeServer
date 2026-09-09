"""POST /trade-result 请求/响应 schema。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.execution import ExecutionDomain


class TradeResult(BaseModel):
    order_id: str
    filled_quantity: int = Field(ge=0)
    filled_price: float = Field(ge=0)
    filled_time: str | None = None
    status: str = Field(pattern=r"^(FILLED|PARTIAL|CANCELLED|REJECTED|NOT_SUBMITTED|EXPIRED_BY_POLICY)$")
    raw_qmt_status: int | None = None
    status_observed_at: datetime | None = None
    expiration_policy_id: str | None = None
    not_submitted_reason: Literal["INSUFFICIENT_CASH", "EXECUTION_WINDOW_EXPIRED"] | None = None
    # 可选（新客户端携带）：order_id 未匹配时用于定位候选订单。
    symbol: str | None = None
    direction: str | None = Field(default=None, pattern=r"^(BUY|SELL)$")
    # live client 下单时捕获的原价/盘口审计字段；均不得使用复权模型价格。
    arrival_reference_price: float | None = Field(default=None, gt=0)
    arrival_reference_time: str | None = None
    submitted_price: float | None = Field(default=None, gt=0)
    submitted_time: str | None = None
    qmt_order_id: str | None = None
    iopv: float | None = Field(default=None, gt=0)
    iopv_time: str | None = None

    @model_validator(mode="after")
    def validate_not_submitted(self) -> "TradeResult":
        if self.status == "NOT_SUBMITTED":
            if self.filled_quantity or self.filled_price or self.qmt_order_id:
                raise ValueError("NOT_SUBMITTED 不得含成交或券商委托号")
            if self.not_submitted_reason is None:
                raise ValueError("NOT_SUBMITTED 必须说明未提交原因")
        elif self.not_submitted_reason is not None:
            raise ValueError("仅 NOT_SUBMITTED 可携带未提交原因")
        return self


class TradeResultRequest(BaseModel):
    trade_date: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")
    # 新客户端必须回显服务端订单域；legacy 模拟盘请求省略时按 paper 处理。
    execution_domain: ExecutionDomain = "paper"
    results: list[TradeResult]


class TradeResultResponseData(BaseModel):
    trade_date: str
    execution_domain: ExecutionDomain = "paper"
    matched_count: int
    unmatched_order_ids: list[str] = Field(default_factory=list)
    rejected_observations: dict[str, str] = Field(default_factory=dict)
    invalidated_attempt_ids: list[str] = Field(default_factory=list)
    local_batch_review_required: bool = False
    # unmatched order_id → 当日库内候选订单（symbol/direction/quantity 相符）。
    # 场景：管线重算换掉 order_id 后客户端按旧 ID 回报（2026-07-02 事故），
    # 候选让人工恢复从『猜』变成『核对后确认』。
    unmatched_candidates: dict[str, list[str]] = Field(default_factory=dict)
