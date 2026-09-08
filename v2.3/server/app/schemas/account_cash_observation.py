"""Observed facts do not carry strategy IDs or a requested target balance."""
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.execution import ExecutionDomain


class AccountCashObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    execution_domain: ExecutionDomain = "paper"
    account_alias: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    source_event_id: str = Field(min_length=1, max_length=200)
    event_type: Literal["DEPOSIT", "WITHDRAWAL", "DIVIDEND", "INTEREST", "FEE", "OTHER"]
    amount: Decimal = Field(max_digits=14, decimal_places=2)
    currency: Literal["CNY"] = "CNY"
    observed_at: str
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # These fields record QMT facts, not requirements for the ledger to match.
    # Even a negative broker cash observation is retained for investigation.
    qmt_cash_balance: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    qmt_available_cash: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("observed_at")
    @classmethod
    def zoned_time(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("observed_at 必须包含时区")
        return value

    @model_validator(mode="after")
    def sign_matches_event(self):
        if self.amount == 0:
            raise ValueError("非零现金变动才能构成现金流事件")
        if self.event_type in {"DEPOSIT", "DIVIDEND", "INTEREST"} and self.amount < 0:
            raise ValueError("入账事件 amount 必须为正")
        if self.event_type in {"WITHDRAWAL", "FEE"} and self.amount > 0:
            raise ValueError("出账事件 amount 必须为负")
        return self


class AccountCashObservationResponseData(BaseModel):
    observation_id: int
    execution_domain: ExecutionDomain
    account_alias: str
    source: str
    source_event_id: str
    evidence_sha256: str
    request_sha256: str
    status: Literal["RECORDED_UNALLOCATED"] = "RECORDED_UNALLOCATED"
    strategy_balance_changed: bool = False
    already_recorded: bool
