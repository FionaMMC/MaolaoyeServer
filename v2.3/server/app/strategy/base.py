"""策略框架契约：Strategy 抽象基类 + RawSignal 输出 dataclass。"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from app.strategy.context import Context


@dataclass(frozen=True)
class RawSignal:
    """策略输出。归集前的原始信号。"""
    symbol: str
    direction: str
    quantity: int
    reference_price: float
    price_offset: float

    def __post_init__(self):
        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"direction must be BUY or SELL, got {self.direction!r}")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.reference_price <= 0:
            raise ValueError(f"reference_price must be positive, got {self.reference_price}")


class Strategy(abc.ABC):
    """所有策略插件必须继承此基类。"""

    name: str = ""
    data_dir: ClassVar[Path | None] = None
    data_files: ClassVar[list[str]] = []

    @abc.abstractmethod
    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        ...
