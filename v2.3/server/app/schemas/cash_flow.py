"""外部现金流 journal API contract。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.execution import ExecutionDomain


class CashFlowRequest(BaseModel):
    execution_domain: ExecutionDomain = "paper"
    account_alias: str = Field(min_length=1, max_length=100)
    instance_id: str = Field(min_length=1, max_length=200)
    event_date: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")
    event_type: Literal["DIVIDEND", "DEPOSIT", "WITHDRAWAL", "OTHER"]
    amount: float
    currency: Literal["CNY"] = "CNY"
    source: str = Field(min_length=1, max_length=100)
    source_event_id: str = Field(min_length=1, max_length=200)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str | None = Field(default=None, max_length=500)

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: float, info):
        if value == 0:
            raise ValueError("cash flow amount 不能为 0")
        event_type = info.data.get("event_type")
        if event_type in {"DIVIDEND", "DEPOSIT"} and value < 0:
            raise ValueError(f"{event_type} amount 必须为正")
        if event_type == "WITHDRAWAL" and value > 0:
            raise ValueError("WITHDRAWAL amount 必须为负")
        return value


class CashFlowResponseData(BaseModel):
    journal_id: int
    execution_domain: ExecutionDomain
    instance_id: str
    event_date: str
    amount: float
    already_applied: bool
    virtual_cash_after: float
