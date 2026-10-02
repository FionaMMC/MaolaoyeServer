"""Incremental ownership movements, not broker cash-flow authorizations."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.execution import ExecutionDomain


class CapitalMovementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    execution_domain: ExecutionDomain = "paper"
    account_alias: str = Field(min_length=1, max_length=100)
    instance_id: str = Field(min_length=1, max_length=200)
    action: Literal["INCREASE", "DECREASE"]
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    event_date: str = Field(pattern=r"^\d{8}$", description="请求计划日期，仅留痕；实际记账日以立即执行时的上海日期为准")
    # Ownership is measured against cash INCLUDING frozen cash. Comparing the
    # sum of ledger balances to QMT available cash counts a freeze twice.
    qmt_cash_balance: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    snapshot_time: str
    source_event_id: str = Field(min_length=1, max_length=200)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str | None = Field(default=None, max_length=500)

    @field_validator("event_date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        datetime.strptime(value, "%Y%m%d")
        return value

    @field_validator("snapshot_time")
    @classmethod
    def zoned_snapshot(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("snapshot_time 必须包含时区")
        return value


class CapitalMovementResponseData(BaseModel):
    journal_id: int
    execution_domain: ExecutionDomain
    account_alias: str
    instance_id: str
    source_event_id: str
    event_type: Literal["CAPITAL_ALLOCATION", "CAPITAL_DEALLOCATION"]
    amount: float
    effective_date: str
    request_sha256: str
    already_applied: bool
    target_policy: Literal["NEXT_UNFROZEN_TARGET"] = "NEXT_UNFROZEN_TARGET"
