"""QMT 账户首次只读快照导入。

一个物理账户可以保留 Hydra 白名单外的人工/其他策略持仓。它们是外部
持仓：初始化时必须被完整上报和审计，但绝不能写入 Hydra 台账。
"""
from __future__ import annotations

import math

from pydantic import BaseModel, Field, field_validator, model_validator

from app.execution import ExecutionDomain


class AccountInitializationRequest(BaseModel):
    execution_domain: ExecutionDomain = "paper"
    account_alias: str = Field(min_length=1, max_length=100)
    qmt_account_id: str = Field(min_length=1, max_length=100)
    instance_id: str = Field(min_length=1, max_length=200)
    qmt_cash: float = Field(ge=0)
    qmt_total_asset: float = Field(gt=0)
    qmt_positions: dict[str, int]
    owned_symbols: list[str] = Field(min_length=1)
    snapshot_time: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("qmt_cash", "qmt_total_asset")
    @classmethod
    def finite_money(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("QMT asset 必须是有限数")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> "AccountInitializationRequest":
        if self.qmt_cash > self.qmt_total_asset:
            raise ValueError("qmt_cash 不能大于 qmt_total_asset")
        if len(self.owned_symbols) != len(set(self.owned_symbols)):
            raise ValueError("owned_symbols 重复")
        if any(
            not isinstance(qty, int) or isinstance(qty, bool) or qty < 0
            for qty in self.qmt_positions.values()
        ):
            raise ValueError("QMT position quantity 非法")
        return self


class AccountInitializationResponseData(BaseModel):
    instance_id: str
    execution_domain: ExecutionDomain
    account_alias: str
    virtual_cash: float
    positions: dict[str, int]
    evidence_sha256: str
    external_position_count: int = 0
    idempotent_replay: bool
