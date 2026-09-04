"""外部现金流 journal API contract。"""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.execution import ExecutionDomain


class CashFlowRequest(BaseModel):
    execution_domain: ExecutionDomain = "paper"
    account_alias: str = Field(min_length=1, max_length=100)
    instance_id: str = Field(min_length=1, max_length=200)
    event_date: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")
    event_type: Literal[
        "DIVIDEND", "DEPOSIT", "WITHDRAWAL", "CAPITAL_ALLOCATION",
        "CAPITAL_DEALLOCATION", "OTHER",
    ]
    amount: float
    qmt_cash: float | None = Field(
        default=None,
        ge=0,
        description="资本划拨时的账户可用现金只读快照；普通现金流不使用",
    )
    snapshot_time: str | None = None
    transition_to_attributed: bool = False
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
        if event_type in {"DIVIDEND", "DEPOSIT", "CAPITAL_ALLOCATION"} and value < 0:
            raise ValueError(f"{event_type} amount 必须为正")
        if event_type in {"WITHDRAWAL", "CAPITAL_DEALLOCATION"} and value > 0:
            raise ValueError(f"{event_type} amount 必须为负")
        return value

    @field_validator("qmt_cash")
    @classmethod
    def validate_qmt_cash(cls, value: float | None):
        if value is not None and not math.isfinite(value):
            raise ValueError("qmt_cash 必须是有限数")
        return value

    @model_validator(mode="after")
    def validate_capital_transfer_evidence(self) -> "CashFlowRequest":
        if self.event_type.startswith("CAPITAL_"):
            if self.qmt_cash is None or not self.snapshot_time:
                raise ValueError("资本划拨必须提供 qmt_cash 和 snapshot_time")
        elif self.qmt_cash is not None or self.snapshot_time is not None:
            raise ValueError("普通现金流不能携带资本划拨快照字段")
        if self.transition_to_attributed and not self.event_type.startswith("CAPITAL_"):
            raise ValueError("账本模式切换只能随资本划拨原子执行")
        return self


class CashFlowResponseData(BaseModel):
    journal_id: int
    execution_domain: ExecutionDomain
    instance_id: str
    event_date: str
    amount: float
    already_applied: bool
    virtual_cash_after: float
    account_ledger_cash_after: float | None = None
    unallocated_cash_after: float | None = None
    ledger_mode_after: str | None = None
