"""QMT 账户首次只读快照导入。

一个物理账户可以保留 Hydra 白名单外的人工/其他策略持仓。它们是外部
持仓：初始化时必须被完整上报和审计，但绝不能写入 Hydra 台账。
"""
from __future__ import annotations

import math
from typing import Literal

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
    ledger_mode: Literal["dedicated", "attributed"] = "dedicated"
    allocated_cash: float | None = Field(default=None, ge=0)
    allocated_positions: dict[str, int] | None = None
    snapshot_time: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("qmt_cash", "qmt_total_asset", "allocated_cash")
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
        if self.ledger_mode == "attributed":
            if self.allocated_cash is None or self.allocated_positions is None:
                raise ValueError(
                    "attributed 账本必须显式提供 allocated_cash 和 allocated_positions"
                )
            if self.allocated_cash > self.qmt_cash:
                raise ValueError("allocated_cash 不能大于 QMT 可用现金")
            owned = set(self.owned_symbols)
            for symbol, quantity in self.allocated_positions.items():
                if symbol not in owned:
                    raise ValueError(f"allocated position {symbol} 不在 owned_symbols")
                if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
                    raise ValueError("allocated position quantity 非法")
                if quantity > self.qmt_positions.get(symbol, 0):
                    raise ValueError(
                        f"allocated position {symbol} 超过 QMT 实际持仓"
                    )
        elif self.allocated_cash is not None or self.allocated_positions is not None:
            raise ValueError("dedicated 初始化不能提供 allocated_*；它认领完整账户快照")
        return self


class AccountInitializationResponseData(BaseModel):
    instance_id: str
    execution_domain: ExecutionDomain
    account_alias: str
    ledger_mode: Literal["dedicated", "attributed"]
    virtual_cash: float
    unallocated_cash: float = 0.0
    positions: dict[str, int]
    evidence_sha256: str
    external_position_count: int = 0
    idempotent_replay: bool


class StrategyLedgerResponseData(BaseModel):
    instance_id: str
    execution_domain: ExecutionDomain
    account_alias: str
    ledger_mode: Literal["legacy", "dedicated", "attributed"]
    virtual_cash: float
    positions: dict[str, int]
    owned_symbols: list[str] | None
    initial_allocated_cash: float | None = None
    cash_flow_totals: dict[str, float]
    last_update: str
