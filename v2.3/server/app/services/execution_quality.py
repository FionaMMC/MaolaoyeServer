"""Read-only, money-weighted observations; never grants capital or places orders.

Positive signed shortfall means worse execution for both BUY and SELL. Prices
are the recorded raw observations, not a reconstructed quote or cached bps.
"""
from __future__ import annotations

from collections import defaultdict
from math import isfinite
from typing import Iterable

from app.models.execution_quality import ExecutionQualityObservation


def _number(value: object, *, positive: bool = False) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(result) or (positive and result <= 0):
        return None
    return result


def _filled_notional(row: ExecutionQualityObservation) -> float:
    price = _number(row.fill_vwap, positive=True)
    quantity = max(int(row.filled_quantity or 0), 0)
    return quantity * price if price is not None else 0.0


def _metric(
    rows: list[ExecutionQualityObservation],
    *,
    numerator: str,
    benchmark: str,
    directional: bool,
    quote_time: str,
) -> dict:
    cost = reference_notional = covered_notional = 0.0
    samples: list[float] = []
    missing_time = 0
    for row in rows:
        quantity = max(int(row.filled_quantity or 0), 0)
        actual = _number(getattr(row, numerator), positive=True)
        base = _number(getattr(row, benchmark), positive=True)
        fill_notional = _filled_notional(row)
        if (
            quantity == 0 or actual is None or base is None or fill_notional <= 0
            or (directional and row.direction not in {"BUY", "SELL"})
        ):
            continue
        sign = -1 if directional and row.direction == "SELL" else 1
        cost += sign * (actual - base) * quantity
        reference_notional += base * quantity
        covered_notional += fill_notional
        samples.append(sign * (actual / base - 1) * 10_000)
        missing_time += not bool(getattr(row, quote_time))
    total_notional = sum(_filled_notional(row) for row in rows)
    return {
        "weighted_bps": cost / reference_notional * 10_000 if reference_notional else None,
        "benchmark_notional": reference_notional,
        "signed_price_difference_amount": cost if samples else None,
        "sample_orders": len(samples),
        "covered_filled_notional": covered_notional,
        "filled_notional_coverage": covered_notional / total_notional if total_notional else None,
        "samples_missing_quote_time": missing_time,
        "worst_observed_order_bps": max(samples) if samples else None,
    }


def _summary(rows: list[ExecutionQualityObservation]) -> dict:
    return {
        "observed_orders": len(rows),
        "filled_orders": sum(int(row.filled_quantity or 0) > 0 for row in rows),
        "filled_quantity": sum(max(int(row.filled_quantity or 0), 0) for row in rows),
        "filled_notional": sum(_filled_notional(row) for row in rows),
        "filled_orders_missing_valid_fill_price": sum(
            int(row.filled_quantity or 0) > 0 and _number(row.fill_vwap, positive=True) is None
            for row in rows
        ),
        "estimated_fees": sum(_number(row.estimated_fees) or 0.0 for row in rows),
        "decision_gap": _metric(
            rows, numerator="arrival_reference_price", benchmark="decision_reference_price",
            directional=True, quote_time="arrival_reference_time",
        ),
        "execution_shortfall": _metric(
            rows, numerator="fill_vwap", benchmark="arrival_reference_price",
            directional=True, quote_time="arrival_reference_time",
        ),
        "etf_premium": _metric(
            rows, numerator="arrival_reference_price", benchmark="iopv",
            directional=False, quote_time="iopv_time",
        ),
    }


def summarize_execution_quality(rows: Iterable[ExecutionQualityObservation]) -> dict:
    """Summarize cumulative per-order rows once, with explicit evidence coverage.

    Caller must select its account/strategy/target/domain scope before calling.
    Existing storage contains a cumulative observation per order, not one row
    per fill. No inference about unobserved orders, liquidity, or capacity is made.
    """
    observed = list(rows)
    summary = _summary(observed)
    cohorts: dict[tuple[str, str, str | None], list[ExecutionQualityObservation]] = defaultdict(list)
    for row in observed:
        cohorts[(row.symbol, row.direction, row.attempt_id)].append(row)
    return {
        "schema_version": 2,
        "aggregation_basis": "benchmark_notional_per_metric",
        **summary,
        # Existing CLI keys stay available; schema_version identifies corrected weighting.
        "orders": summary["observed_orders"],
        "weighted_decision_gap_bps": summary["decision_gap"]["weighted_bps"],
        "weighted_execution_shortfall_bps": summary["execution_shortfall"]["weighted_bps"],
        "weighted_premium_bps": summary["etf_premium"]["weighted_bps"],
        "missing_arrival_reference": sum(
            _number(row.arrival_reference_price, positive=True) is None for row in observed
        ),
        "missing_iopv": sum(_number(row.iopv, positive=True) is None for row in observed),
        "cohorts": [
            {"symbol": symbol, "direction": direction, "attempt_id": attempt, **_summary(group)}
            for (symbol, direction, attempt), group in sorted(
                cohorts.items(), key=lambda item: tuple(value or "" for value in item[0])
            )
        ],
        "capital_scaling": {
            "status": "DESCRIPTIVE_ONLY",
            "capacity_estimate": None,
            "automatic_capital_change": False,
            "limitations": [
                "No calibrated impact model or approved capital threshold is inferred.",
                "Quote timestamps, freshness and source provenance require separate validation.",
                "Unfilled orders are excluded from price slippage, not assumed zero-cost.",
                "Estimated fees are separate and not confirmed broker fees.",
                "Capital, market liquidity and participation data are not joined in this report.",
            ],
        },
    }
