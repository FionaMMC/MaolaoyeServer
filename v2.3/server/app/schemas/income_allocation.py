"""Explicit ownership of received income, not an inferred dividend entitlement."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.execution import ExecutionDomain


class IncomeShare(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    instance_id: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)


class IncomeAllocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    execution_domain: ExecutionDomain = "paper"
    account_alias: str = Field(min_length=1, max_length=100)
    observation_id: int = Field(gt=0)
    event_date: str = Field(pattern=r"^\d{8}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    allocations: list[IncomeShare] = Field(min_length=1, max_length=1000)

    @field_validator("event_date")
    @classmethod
    def valid_date(cls, value):
        datetime.strptime(value, "%Y%m%d")
        return value

    @model_validator(mode="after")
    def unique_owners(self):
        owners = [share.instance_id for share in self.allocations]
        if len(owners) != len(set(owners)):
            raise ValueError("每个策略只能出现一次")
        return self


class IncomeAllocationResponse(BaseModel):
    observation_id: int
    execution_domain: ExecutionDomain
    account_alias: str
    request_sha256: str
    status: str = "ALLOCATED"
    already_applied: bool
    allocations: list[dict]
