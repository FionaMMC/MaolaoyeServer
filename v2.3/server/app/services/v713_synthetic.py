"""Read-only synthetic V713 ledger over two live legs.

Design: docs/V713_STOCK_LEG_SYNTHETIC_DESIGN_20261023.md section 6. It holds units of
the stock leg and of Hydra at each leg's flow-adjusted unit NAV and moves them only at
session closes. It never creates targets or orders.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path

from app.oms.v713_stock_leg import STATE_STOCK_WEIGHT

AUX = "AUX_HYDRA"


@dataclass(frozen=True)
class SyntheticPolicy:
    policy_id: str
    stock_instance: str
    hydra_instance: str
    label: str
    base_value: float = 400_000.0
    cost_bps: tuple = (0.0, 20.0, 50.0)
    primary_bps: float = 20.0
    liquidated_stock_weight: float = 0.01
    review_after_sessions: int = 10


@dataclass(frozen=True)
class MonthState:
    signal_date: str
    sleeve: str
    anchor_date: str  # the close at which this month's move applies: its first execution session


def load_synthetic_policy(path) -> SyntheticPolicy:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    policy = SyntheticPolicy(**{**payload, "cost_bps": tuple(float(b) for b in payload["cost_bps"])})
    if not policy.policy_id.startswith("V713_SYNTHETIC_"):
        raise ValueError("synthetic policy requires a versioned V713_SYNTHETIC_ policy_id")
    if policy.primary_bps not in policy.cost_bps or policy.base_value <= 0:
        raise ValueError("the primary cost must be a reported line and the base positive")
    if not 0 < policy.liquidated_stock_weight < .05 or policy.review_after_sessions < 1:
        raise ValueError("invalid liquidation completion rule")
    return policy


def unit_nav(snapshots) -> dict[str, float]:
    """Chain flow-adjusted daily returns (``PerfSnapshot.daily_return``); the first snapshot is 1."""
    units, value = {}, None
    for date, daily_return in sorted(snapshots):
        if value is None:
            value = 1.0
        elif daily_return is None or not math.isfinite(daily_return) or daily_return <= -1:
            raise ValueError(f"missing or invalid daily return on {date}")
        else:
            value *= 1 + daily_return
        units[date] = value
    return units


def rebalance(values, cash, stock_weight, rates):
    """Self-financing two-leg move with the Oct-16 overlay convention.

    Sells first, each side at its own extra rate; buys are scaled together when the
    net sale proceeds cannot fund them. Returns (values, cash, cost, traded).
    """
    values = list(values)
    before = cash + values[0] + values[1]
    delta = [stock_weight * before - values[0], (1 - stock_weight) * before - values[1]]
    cost = traded = 0.0
    for i in range(2):
        if delta[i] < 0:
            sale = -delta[i]
            values[i] -= sale
            cash += sale * (1 - rates[i])
            cost += sale * rates[i]
            traded += sale
    need = sum(delta[i] * (1 + rates[i]) for i in range(2) if delta[i] > 0)
    scale = min(1.0, max(0.0, cash) / need) if need else 0.0
    for i in range(2):
        if delta[i] > 0:
            purchase = delta[i] * scale
            values[i] += purchase
            cash -= purchase * (1 + rates[i])
            cost += purchase * rates[i]
            traded += purchase
    return values, cash, cost, traded
