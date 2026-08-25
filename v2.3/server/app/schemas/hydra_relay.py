"""Hydra target → rebalance → attempt contract。"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.execution import ExecutionDomain


class HydraWeight(BaseModel):
    code: str = Field(pattern=r"^\d{6}\.(SH|SZ)$")
    weight: float = Field(gt=0, le=1)

    @field_validator("weight")
    @classmethod
    def finite_weight(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("weight 必须是有限数")
        return value


class HydraTargetRequest(BaseModel):
    execution_domain: ExecutionDomain = "paper"
    account_alias: str = Field(min_length=1, max_length=100)
    instance_id: str = Field(min_length=1, max_length=200)
    strategy_version: str = Field(min_length=1, max_length=200)
    publisher_source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    decision_date: str = Field(pattern=r"^\d{8}$")
    as_of_date: str = Field(pattern=r"^\d{8}$")
    execution_date: str = Field(pattern=r"^\d{8}$")
    research_input_hashes: dict[str, str]
    input_hashes: dict[str, str]
    weights: list[HydraWeight] = Field(min_length=1)
    cash_buffer_weight: float = Field(ge=0, lt=1)
    basket_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    buy_price_offset_bps: float = Field(default=50.0, ge=0, le=50)
    sell_price_offset_bps: float = Field(default=50.0, ge=0, le=50)

    @model_validator(mode="after")
    def validate_contract(self) -> "HydraTargetRequest":
        codes = [item.code for item in self.weights]
        if len(codes) != len(set(codes)):
            raise ValueError("Hydra target code 重复")
        if abs(sum(item.weight for item in self.weights) - 1.0) > 1e-8:
            raise ValueError("Hydra target weight 必须合计为 1")
        required_hashes = {
            "model_hfq", "execution_raw", "corporate_actions", "trading_calendar",
        }
        if set(self.input_hashes) != required_hashes:
            raise ValueError(f"input_hashes 必须恰好包含 {sorted(required_hashes)}")
        if not all(
            len(value) == 64 and all(char in "0123456789abcdef" for char in value)
            for value in self.input_hashes.values()
        ):
            raise ValueError("input_hashes 必须是 lowercase SHA-256")
        if not self.research_input_hashes or not all(
            key
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
            for key, value in self.research_input_hashes.items()
        ):
            raise ValueError("research_input_hashes 必须是非空的 lowercase SHA-256 映射")
        if self.as_of_date > self.decision_date:
            raise ValueError("as_of_date 不能晚于 decision_date")
        if self.execution_date <= self.decision_date:
            raise ValueError("execution_date 必须晚于 decision_date")
        if hydra_basket_hash(self) != self.basket_sha256:
            raise ValueError("basket_sha256 与 target 内容不一致")
        return self


def hydra_basket_hash(target: HydraTargetRequest | dict) -> str:
    payload = target if isinstance(target, dict) else target.model_dump()
    canonical = {
        "strategy_version": payload["strategy_version"],
        "publisher_source_commit": payload["publisher_source_commit"],
        "decision_date": payload["decision_date"],
        "as_of_date": payload["as_of_date"],
        "execution_date": payload["execution_date"],
        "research_input_hashes": dict(sorted(payload["research_input_hashes"].items())),
        "input_hashes": dict(sorted(payload["input_hashes"].items())),
        "weights": sorted(
            [
                {
                    "code": item["code"] if isinstance(item, dict) else item.code,
                    "weight": float(
                        item["weight"] if isinstance(item, dict) else item.weight
                    ),
                }
                for item in payload["weights"]
            ],
            key=lambda item: item["code"],
        ),
        "cash_buffer_weight": float(payload["cash_buffer_weight"]),
    }
    body = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


class HydraRetryRequest(BaseModel):
    execution_domain: ExecutionDomain = "paper"
    account_alias: str = Field(min_length=1, max_length=100)
    rebalance_id: str
    trade_date: str = Field(pattern=r"^\d{8}$")
    execution_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    actual_cash: float = Field(ge=0)
    actual_positions: dict[str, int]
    reconciliation_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HydraAttemptCloseRequest(BaseModel):
    execution_domain: ExecutionDomain = "paper"
    account_alias: str = Field(min_length=1, max_length=100)
    attempt_id: str
    actual_cash: float = Field(ge=0)
    actual_positions: dict[str, int]
    reconciliation_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HydraAttemptCloseResponseData(BaseModel):
    target_id: str
    rebalance_id: str
    attempt_id: str
    execution_domain: ExecutionDomain
    status: Literal["COMPLETE", "RESIDUAL"]
    residual_after: dict[str, int]


class HydraRelayResponseData(BaseModel):
    target_id: str
    rebalance_id: str
    attempt_id: str
    batch_id: str
    batch_sha256: str
    execution_domain: ExecutionDomain
    trade_date: str
    order_count: int
    idempotent_replay: bool = False
