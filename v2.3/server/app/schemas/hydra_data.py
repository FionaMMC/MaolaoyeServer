"""Hydra 双数据链不可变批次 manifest。"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HydraDataStream = Literal[
    "hydra_model_hfq",
    "hydra_execution_raw",
    "hydra_corporate_actions",
    "hydra_trading_calendar",
]


class HydraDataManifest(BaseModel):
    # Manifest 是研究端与执行端之间的审计契约。允许并保留向前兼容的审计
    # 字段，避免一次正常的 stage/install 把研究端提供的血缘说明静默丢掉。
    model_config = ConfigDict(extra="allow")

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
    symbols: list[str] | None = None
    executable_symbols: list[str] | None = None
    research_only_symbols: list[str] | None = None
    notes: list[str] | None = None
    updated_at_utc: str | None = None

    @model_validator(mode="after")
    def validate_symbol_provenance(self) -> "HydraDataManifest":
        named_sets: dict[str, set[str]] = {}
        for field_name in (
            "symbols", "executable_symbols", "research_only_symbols",
        ):
            values = getattr(self, field_name)
            if values is None:
                continue
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} 含重复标的")
            invalid = sorted(
                value for value in values
                if not isinstance(value, str)
                or not re.fullmatch(r"\d{6}\.(SH|SZ)", value)
            )
            if invalid:
                raise ValueError(f"{field_name} 含非法 QMT ETF code: {invalid}")
            named_sets[field_name] = set(values)
        if self.symbols is not None and len(self.symbols) != self.symbol_count:
            raise ValueError("symbols 数量与 symbol_count 不一致")
        executable = named_sets.get("executable_symbols", set())
        research_only = named_sets.get("research_only_symbols", set())
        if executable & research_only:
            raise ValueError("executable_symbols 与 research_only_symbols 必须互斥")
        if self.symbols is not None and (executable or research_only):
            if executable | research_only != named_sets["symbols"]:
                raise ValueError("可交易/仅研究标的未完整覆盖 symbols")
        return self


class HydraDataInstallResult(BaseModel):
    stream: HydraDataStream
    file_sha256: str
    manifest_sha256: str
    as_of_date: str
    row_count: int
    symbol_count: int
    installed: bool
    batch_dir: str
