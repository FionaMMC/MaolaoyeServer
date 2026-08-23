"""Hydra 双数据链不可变批次 manifest。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

HydraDataStream = Literal[
    "hydra_model_hfq",
    "hydra_execution_raw",
    "hydra_corporate_actions",
    "hydra_trading_calendar",
]


class HydraDataManifest(BaseModel):
    schema_version: Literal[1] = 1
    stream: HydraDataStream
    source: str = Field(min_length=1, max_length=100)
    adjustment: Literal["back", "none", "corporate_actions", "calendar"]
    as_of_date: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")
    fetched_at: str = Field(min_length=1)
    producer_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # 一个经过验证的“本期无公司行动”空表也是必要证据；价格/日历流仍由
    # frame validator 强制非空并覆盖 as_of_date。
    row_count: int = Field(ge=0)
    symbol_count: int = Field(ge=0)


class HydraDataInstallResult(BaseModel):
    stream: HydraDataStream
    file_sha256: str
    manifest_sha256: str
    as_of_date: str
    row_count: int
    symbol_count: int
    installed: bool
    batch_dir: str
