"""Explicit orders, never an unbounded force flag on normal requests."""
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EmergencyOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ)$")
    direction: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0, strict=True)
    limit_price: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_lot_tick(self):
        if self.direction == "BUY" and self.quantity % 100:
            raise ValueError("买入必须为 100 份整数倍")
        if Decimal(str(self.limit_price)) % Decimal("0.001"):
            raise ValueError("限价必须对齐 0.001 tick")
        return self


class EmergencyStageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    execution_domain: Literal["live"] = "live"
    account_alias: str = Field(min_length=1, max_length=100)
    instance_id: str = Field(min_length=1, max_length=200)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    operator: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    confirm_emergency: Literal[True]
    trade_date: str = Field(pattern=r"^\d{8}$")
    execution_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_calendar_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    orders: list[EmergencyOrder] = Field(min_length=1, max_length=100)

    @field_validator("trade_date")
    @classmethod
    def valid_date(cls, value):
        datetime.strptime(value, "%Y%m%d")
        return value

    @model_validator(mode="after")
    def unique_symbols(self):
        if len({order.symbol for order in self.orders}) != len(self.orders):
            raise ValueError("同一紧急批次每个证券只允许一笔明确方向的订单")
        self.orders.sort(key=lambda order: order.symbol)
        return self


class EmergencyResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    execution_domain: Literal["live"] = "live"
    account_alias: str = Field(min_length=1, max_length=100)
    instance_id: str = Field(min_length=1, max_length=200)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    reason: str = Field(min_length=1, max_length=2000)
    confirm_new_plan_required: Literal[True]
