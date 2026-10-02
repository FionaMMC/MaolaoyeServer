"""ETF 整手约束下的资金规模与仓位失衡 preflight。"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CapitalPreflightResult:
    capital: float
    expected_names: int
    held_names: int
    name_coverage: float
    invested: float
    exposure: float
    cash: float
    max_abs_weight_error_pp: float
    total_abs_weight_error_pp: float
    shares: dict[str, int]

    def to_dict(self) -> dict:
        return asdict(self)


def validate_inputs(
    weights: dict[str, float], prices: dict[str, float], cash_buffer: float,
) -> None:
    if not weights or set(weights) != set(prices):
        raise ValueError("weights/prices 必须非空且代码集合完全一致")
    if any(not math.isfinite(value) or value <= 0 for value in weights.values()):
        raise ValueError("weights 必须是有限正数")
    if abs(sum(weights.values()) - 1.0) > 1e-8:
        raise ValueError("weights 必须合计为 1")
    if any(not math.isfinite(value) or value <= 0 for value in prices.values()):
        raise ValueError("prices 必须是有限正数")
    if not 0 <= cash_buffer < 1:
        raise ValueError("cash_buffer 必须在 [0, 1)")


def analyze_capital(
    weights: dict[str, float],
    prices: dict[str, float],
    capital: float,
    *,
    cash_buffer: float = 0.0,
    lot_size: int = 100,
) -> CapitalPreflightResult:
    validate_inputs(weights, prices, cash_buffer)
    if not math.isfinite(capital) or capital <= 0 or lot_size <= 0:
        raise ValueError("capital/lot_size 必须为正")
    investable = capital * (1 - cash_buffer)
    shares = {
        code: int(math.floor(investable * weight / prices[code] / lot_size) * lot_size)
        for code, weight in weights.items()
    }
    notionals = {code: shares[code] * prices[code] for code in weights}
    invested = sum(notionals.values())
    actual_weights = {code: notionals[code] / capital for code in weights}
    errors = {
        code: abs(actual_weights[code] - weights[code]) * 100
        for code in weights
    }
    held = sum(qty > 0 for qty in shares.values())
    return CapitalPreflightResult(
        capital=capital,
        expected_names=len(weights),
        held_names=held,
        name_coverage=held / len(weights),
        invested=round(invested, 6),
        exposure=invested / capital,
        cash=round(capital - invested, 6),
        max_abs_weight_error_pp=max(errors.values()),
        total_abs_weight_error_pp=sum(errors.values()),
        shares=shares,
    )


def minimum_capital_for_name_coverage(
    weights: dict[str, float],
    prices: dict[str, float],
    coverage: float,
    *,
    cash_buffer: float = 0.0,
    lot_size: int = 100,
) -> float:
    validate_inputs(weights, prices, cash_buffer)
    if not 0 < coverage <= 1:
        raise ValueError("coverage 必须在 (0, 1]")
    required = sorted(
        lot_size * prices[code] / (weights[code] * (1 - cash_buffer))
        for code in weights
    )
    rank = max(1, math.ceil(len(required) * coverage))
    return required[rank - 1]
