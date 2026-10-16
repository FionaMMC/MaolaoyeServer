"""Explicit reviewed accounting commands; no inferred money or corporate actions."""
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AccountingCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    execution_domain: Literal["paper", "live"] = "live"
    account_alias: str = Field(min_length=1)
    event_id: str = Field(min_length=1, max_length=200)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operator: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=4, max_length=1000)


class FeeAdjustmentRequest(AccountingCommand):
    order_id: str
    filled_quantity: int = Field(gt=0)
    actual_cumulative_fees: Decimal = Field(ge=0, max_digits=16, decimal_places=2)


class CapitalTransferRequest(AccountingCommand):
    from_instance_id: str
    to_instance_id: str
    amount: Decimal = Field(gt=0, max_digits=16, decimal_places=2)

    @model_validator(mode="after")
    def distinct(self):
        if self.from_instance_id == self.to_instance_id:
            raise ValueError("capital transfer requires two different owners")
        return self


class CorporateActionIssueRequest(AccountingCommand):
    instance_id: str
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ)$")
    action_type: Literal["DIVIDEND", "TAX", "STOCK_DISTRIBUTION", "SPLIT", "OTHER"]


class CorporateActionResolutionRequest(AccountingCommand):
    instance_id: str
    issue_id: str
    expected_quantity: int = Field(ge=0)
    quantity_delta: int = 0
    cash_delta: Decimal = Field(default=Decimal(0), max_digits=16, decimal_places=2)
    # Explicit source snapshot validates the resulting physical resources.
    broker_snapshot_id: str = Field(min_length=1)
    expected_cash: Decimal = Field(max_digits=16, decimal_places=2)

    @field_validator("quantity_delta", mode="before")
    @classmethod
    def integer_delta(cls, value):
        if type(value) is not int:
            raise ValueError("quantity_delta must be an integer")
        return value
