"""Read-only research math for a unitised stock/Hydra combination.

No broker, OMS, environment file, market downloader, or production DB imports.
Period inputs must already be aligned on the remote research machine. A period
starts AFTER its signal is known. Month-end clocks are research assumptions,
not evidence that a closing-price order could actually have been executed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics


STATE_STOCK_WEIGHT = {"TOP50": 1.0, "T1_5050": 0.5, "AUX_HYDRA": 0.0}


def finite(value, name):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def timestamp(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def unitize_events(events):
    """Exact boundary-flow unitisation; internal fees/dividends stay in NAV.

    Each event supplies value_before_flow, external_flow, value_after_flow at
    the same valuation. A daily total without a before-flow valuation cannot
    be passed off as an exact intraday time-weighted return.
    """
    units = None
    previous = None
    output = []
    for event in events:
        time = timestamp(event["timestamp"])
        if previous is not None and time <= previous:
            raise ValueError("event timestamps must be strictly increasing")
        before = finite(event["value_before_flow"], "value_before_flow")
        flow = finite(event["external_flow"], "external_flow")
        after = finite(event["value_after_flow"], "value_after_flow")
        if before <= 0 or after <= 0:
            raise ValueError("positive before/after NAV required; closed funds start a new series")
        if not math.isclose(after, before + flow, rel_tol=1e-11, abs_tol=1e-7):
            raise ValueError("cash-flow identity does not reconcile")
        if units is None:
            units = before  # unit price = 1 at the first observed boundary
        price = before / units
        units += flow / price
        if units <= 0 or not math.isclose(after / units, price, rel_tol=1e-11):
            raise ValueError("invalid fund units")
        output.append({"timestamp": str(event["timestamp"]), "unit_nav": price, "units": units})
        previous = time
    return output


def lookthrough_weights(sleeve, stock_cash=0.01, hydra_cash=0.0, outer_cash=0.0):
    """Account for cash embedded in each unit NAV exactly once."""
    values = [finite(v, "cash fraction") for v in (stock_cash, hydra_cash, outer_cash)]
    if any(v < 0 or v > 1 for v in values):
        raise ValueError("cash fractions must be in [0, 1]")
    stock_cash, hydra_cash, outer_cash = values
    stock = STATE_STOCK_WEIGHT[sleeve] * (1 - outer_cash)
    hydra = (1 - STATE_STOCK_WEIGHT[sleeve]) * (1 - outer_cash)
    return {"stocks": stock * (1 - stock_cash), "hydra_assets": hydra * (1 - hydra_cash),
            "cash": outer_cash + stock * stock_cash + hydra * hydra_cash}


def unique_physical_capital(sources):
    """Referencing a physical source in several research books adds no assets."""
    unique = {}
    for item in sources:
        source, amount = str(item["source_id"]), finite(item["capital"], "capital")
        if amount < 0:
            raise ValueError("negative capital")
        if source in unique and not math.isclose(unique[source], amount, rel_tol=1e-12):
            raise ValueError("inconsistent valuation of the same physical source")
        unique[source] = amount
    return sum(unique.values())


def metrics(values, periods_per_year=12):
    returns = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    peak, drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    n = len(returns)
    return {"periods": n, "cumulative_return": values[-1] / values[0] - 1,
            "cagr_period_based": (values[-1] / values[0]) ** (periods_per_year / n) - 1 if n else None,
            "period_end_max_drawdown": drawdown,
            "annualized_period_volatility": statistics.stdev(returns) * math.sqrt(periods_per_year) if n > 1 else None}


def simulate_periods(rows, initial_capital=400_000, rebalance="monthly", switch_bps=0,
                     fill_fraction=1, lag_periods=0, outer_cash_weight=0,
                     fixed_stock_weight=None, periods_per_year=None):
    """Hold units between explicitly supplied valuation/rebalance boundaries.

    switch_bps is a hypothetical EXTRA overlay cost per gross fund-unit trade,
    NOT a fitted security-level switching cost. Internal leg costs must already
    be in the leg returns. Costs are funded by sells/cash before buys; if cash
    is insufficient, all desired buys are scaled together. Partial fills leave
    actual residual exposures. lag_periods=1 with monthly inputs means ONE
    MONTH, not a one-day execution delay. Initial seeding is free and excluded
    from turnover; tests of actual entry need security-level execution data.
    outer_cash_weight is a pre-fee target, not a mandatory minimum cash floor.
    """
    capital = finite(initial_capital, "initial_capital")
    rate = finite(switch_bps, "switch_bps") / 10_000
    fraction = finite(fill_fraction, "fill_fraction")
    outer = finite(outer_cash_weight, "outer_cash_weight")
    if capital <= 0 or not 0 <= rate < 1 or not 0 <= fraction <= 1 or not 0 <= outer <= 1:
        raise ValueError("invalid capital/cost/fill/cash setting")
    if not isinstance(lag_periods, int) or lag_periods < 0:
        raise ValueError("lag_periods must be a nonnegative integer")
    if rebalance not in {"monthly", "daily", "state_only"}:
        raise ValueError("unsupported rebalance rule")
    if fixed_stock_weight is not None and not 0 <= finite(fixed_stock_weight, "fixed_stock_weight") <= 1:
        raise ValueError("fixed stock weight must be in [0, 1]")
    if not rows:
        raise ValueError("empty aligned panel")
    if periods_per_year is None:
        periods_per_year = 252 if all((timestamp(r["end"]) - timestamp(r["start"])).days <= 7 for r in rows) else 12
    if periods_per_year not in {12, 252}:
        raise ValueError("specify monthly (12) or daily (252) input cadence")
    if rebalance == "daily" and periods_per_year != 252:
        raise ValueError("daily reset requires daily inputs; do not fabricate daily returns from monthly data")
    prices = [1.0, 1.0]
    units = [0.0, 0.0]
    cash = capital
    values = [capital]
    result = []
    previous_end = None
    last_executed_sleeve = None
    last_rebalance_month = None
    max_error = 0.0
    for index, row in enumerate(rows):
        start, end, known = (timestamp(row[key]) for key in ("start", "end", "known_at"))
        if not known <= start < end:
            raise ValueError("signal must be known no later than the start of its return interval")
        if previous_end is not None and start != previous_end:
            raise ValueError("periods must be ordered, contiguous and nonoverlapping")
        sleeve = str(row["sleeve"])
        if sleeve not in STATE_STOCK_WEIGHT:
            raise ValueError("unknown sleeve")
        returns = [finite(row[key], key) for key in ("stock_return", "hydra_return")]
        if any(value <= -1 for value in returns):
            raise ValueError("leg return must exceed -100%")
        # Before a delayed first signal, retain the originally observed state.
        execute_index = max(0, index - lag_periods)
        execute_sleeve = str(rows[execute_index]["sleeve"])
        weight = STATE_STOCK_WEIGHT[execute_sleeve] if fixed_stock_weight is None else float(fixed_stock_weight)
        target = [weight * (1 - outer), (1 - weight) * (1 - outer)]
        before = cash + sum(unit * price for unit, price in zip(units, prices))
        cost, traded = 0.0, 0.0
        pre_weights = [unit * price / before for unit, price in zip(units, prices)]
        if index == 0:
            units = [before * target[i] / prices[i] for i in range(2)]
            cash = before * outer
        elif (rebalance == "daily" or execute_sleeve != last_executed_sleeve
              or (rebalance == "monthly" and (end.year, end.month) != last_rebalance_month)):
            delta = [fraction * (before * target[i] - units[i] * prices[i]) for i in range(2)]
            # Sell before buying. No leverage and no fabricated cash top-up.
            for i in range(2):
                if delta[i] < 0:
                    sale = -delta[i]
                    units[i] -= sale / prices[i]
                    cash += sale * (1 - rate)
                    cost += sale * rate
                    traded += sale
            buy_total = sum(max(value, 0.0) for value in delta)
            scale = min(1.0, max(0.0, cash) / (buy_total * (1 + rate))) if buy_total else 0.0
            for i in range(2):
                if delta[i] > 0:
                    purchase = delta[i] * scale
                    units[i] += purchase / prices[i]
                    cash -= purchase * (1 + rate)
                    cost += purchase * rate
                    traded += purchase
        after_trades = cash + sum(unit * price for unit, price in zip(units, prices))
        error = abs(after_trades - (before - cost))
        max_error = max(max_error, error / capital)
        if cash < -1e-7 or min(units) < -1e-9 or not math.isclose(after_trades, before - cost, rel_tol=1e-11, abs_tol=1e-7):
            raise AssertionError("self-financing identity failed")
        start_values = [units[i] * prices[i] for i in range(2)]
        for i in range(2):
            prices[i] *= 1 + returns[i]
        nav = cash + sum(units[i] * prices[i] for i in range(2))
        if not math.isfinite(nav) or nav <= 0:
            raise ValueError("invalid simulated NAV")
        result.append({"start": str(row["start"]), "end": str(row["end"]),
                       "desired_sleeve": sleeve, "executed_sleeve": execute_sleeve,
                       "nav": nav, "unit_nav": nav / capital, "return": nav / before - 1,
                       "stock_weight": units[0] * prices[0] / nav,
                       "hydra_weight": units[1] * prices[1] / nav, "cash_weight": cash / nav,
                       "start_stock_weight": start_values[0] / after_trades,
                       "start_hydra_weight": start_values[1] / after_trades,
                       "pre_return_stock_value": start_values[0],
                       "pre_return_hydra_value": start_values[1],
                       "pre_return_cash_value": cash,
                       "pre_stock_weight": pre_weights[0],
                       "stock_scale_to_200k": start_values[0] / 200_000,
                       "traded_notional": traded, "cost": cost,
                       "traded_notional_over_start_nav": traded / before,
                       "cost_over_start_nav": cost / before})
        values.append(nav)
        last_executed_sleeve, previous_end = execute_sleeve, end
        last_rebalance_month = (end.year, end.month)
    return {"summary": {**metrics(values, periods_per_year), "periods_per_year": periods_per_year,
                        "start": str(rows[0]["start"]), "end": str(rows[-1]["end"]), "final_nav": values[-1],
                        "state_counts": dict(Counter(str(row["sleeve"]) for row in rows)),
                        "rebalance": rebalance, "switch_bps": switch_bps,
                        "fill_fraction": fraction, "lag_periods": lag_periods,
                        "outer_cash_weight": outer, "initial_capital": capital,
                        "gross_overlay_turnover_over_initial_capital": sum(row["traded_notional"] for row in result) / capital,
                        "overlay_cost_over_initial_capital": sum(row["cost"] for row in result) / capital,
                        "sum_gross_overlay_turnover_over_period_start_nav": sum(row["traded_notional_over_start_nav"] for row in result),
                        "sum_overlay_cost_over_period_start_nav": sum(row["cost_over_start_nav"] for row in result),
                        "max_self_financing_error_over_initial_capital": max_error,
                        "min_cash_weight": min(row["cash_weight"] for row in result)}, "rows": result}


def compare(left, right):
    a, b = left["rows"], right["rows"]
    if len(a) != len(b) or any(x["start"] != y["start"] or x["end"] != y["end"] for x, y in zip(a, b)):
        raise ValueError("comparison must use identical periods")
    cadence = left["summary"]["periods_per_year"]
    if cadence != right["summary"]["periods_per_year"]:
        raise ValueError("comparison must use identical cadence")
    differences = [x["return"] - y["return"] for x, y in zip(a, b)]
    return {"relative_terminal_nav": a[-1]["unit_nav"] / b[-1]["unit_nav"] - 1,
            "cumulative_return_pp_difference": a[-1]["unit_nav"] - b[-1]["unit_nav"],
            "annualized_period_tracking_error": statistics.stdev(differences) * math.sqrt(cadence) if len(a) > 1 else None,
            "max_abs_period_return_difference": max(abs(value) for value in differences)}


def run_suite(rows):
    cases = {"original_monthly": simulate_periods(rows),
             "original_state_only": simulate_periods(rows, rebalance="state_only"),
             "fixed_5050_monthly": simulate_periods(rows, fixed_stock_weight=0.5),
             "reference_200k": simulate_periods(rows, initial_capital=200_000),
             "overlay_5bps": simulate_periods(rows, switch_bps=5),
             "overlay_20bps": simulate_periods(rows, switch_bps=20),
             "overlay_50bps": simulate_periods(rows, switch_bps=50),
             "one_month_signal_lag": simulate_periods(rows, lag_periods=1),
             "half_fill_each_month": simulate_periods(rows, fill_fraction=0.5)}
    if all("rb_hydra_return" in row for row in rows):
        rb_rows = [{**row, "hydra_return": row["rb_hydra_return"]} for row in rows]
        cases["rb_monthly"] = simulate_periods(rb_rows)
    for source in ("stock_net_proxy_return", "stock_100k_return", "stock_200k_return", "stock_400k_return"):
        if all(source in row for row in rows):
            changed = [{**row, "stock_return": row[source]} for row in rows]
            cases[source.removesuffix("_return") + "_monthly"] = simulate_periods(changed)
    baseline = cases["original_monthly"]
    summaries = {name: case["summary"] for name, case in cases.items()}
    differences = {name: compare(case, baseline) for name, case in cases.items() if name != "original_monthly"}
    identities = {}
    if all("original_baseline_gross_return" in row for row in rows):
        identities["max_abs_baseline_gross_period_error"] = max(
            abs(actual["return"] - float(source["original_baseline_gross_return"]))
            for actual, source in zip(baseline["rows"], rows))
    if all("original_baseline_net_return" in row for row in rows):
        values = [1.0]
        for row in rows:
            values.append(values[-1] * (1 + float(row["original_baseline_net_return"])))
        identities["reported_baseline_net_cost_convention_metrics"] = metrics(values)
        if "stock_net_proxy_monthly" in cases:
            identities["stock_cost_scaled_by_state_vs_reported_baseline_terminal_relative_nav"] = (
                cases["stock_net_proxy_monthly"]["rows"][-1]["unit_nav"] / values[-1] - 1)
    return {"cases": summaries, "vs_original_monthly": differences, "identities": identities,
            "cash_lookthrough": {state: lookthrough_weights(state) for state in STATE_STOCK_WEIGHT},
            "details": {name: case["rows"] for name, case in cases.items()}}


def paired_scale_sensitivity(records):
    """Compare matched stock-only windows, not differences of medians."""
    groups = {}
    for row in records:
        key = (str(row["scenario"]), int(row["capital"]))
        window = (str(row["start"]), str(row["date"]))
        nav = 1 + finite(row["relative_nav_deviation"], "relative_nav_deviation")
        if nav <= 0 or window in groups.setdefault(key, {}):
            raise ValueError("invalid or duplicate stock scale window")
        groups[key][window] = nav
    result = []
    scenarios = sorted({key[0] for key in groups})
    for scenario in scenarios:
        reference = groups[(scenario, 200_000)]
        for capital in (100_000, 400_000):
            alternative = groups[(scenario, capital)]
            if reference.keys() != alternative.keys() or not reference:
                raise ValueError("stock scale comparisons require identical nonempty windows")
            differences = [alternative[key] / reference[key] - 1 for key in sorted(reference)]
            result.append({"scenario": scenario, "stock_capital": capital, "reference_stock_capital": 200_000,
                           "matched_windows": len(differences), "median_relative_terminal_nav": statistics.median(differences),
                           "min_relative_terminal_nav": min(differences), "max_relative_terminal_nav": max(differences)})
    return {"basis": "Matched overlapping 12-month stock-only sensitivity accounts; not dynamic-state execution or independent statistical samples",
            "comparisons": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="aligned period JSON, raw data stays remote")
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--details", required=True, type=Path, help="must remain in remote research-runs")
    parser.add_argument("--scale-cases", type=Path, help="optional remote stock rolling_metrics.parquet")
    args = parser.parse_args()
    remote_root = Path("/opt/qmt-server/private/research-runs").resolve()
    if not args.details.resolve().is_relative_to(remote_root):
        raise ValueError("period-level details must stay in the remote research-runs directory")
    payload = json.loads(args.input.read_text())
    if not isinstance(payload, dict) or payload.get("metadata", {}).get("periods_per_year") != 12:
        raise ValueError("this research suite requires an explicitly monthly panel (periods_per_year=12)")
    rows = payload["rows"] if isinstance(payload, dict) else payload
    result = run_suite(rows)
    args.details.write_text(json.dumps(result.pop("details"), indent=2, allow_nan=False))
    result["input"] = {"path": str(args.input), "sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
                       "periods": len(rows), "metadata": payload.get("metadata", {}) if isinstance(payload, dict) else {}}
    result["engine_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["details_sha256"] = hashlib.sha256(args.details.read_bytes()).hexdigest()
    if args.scale_cases:
        import pandas as pd
        result["paired_stock_scale_sensitivity"] = paired_scale_sensitivity(pd.read_parquet(args.scale_cases).to_dict("records"))
        result["paired_stock_scale_sensitivity"]["source"] = {"path": str(args.scale_cases), "sha256": hashlib.sha256(args.scale_cases.read_bytes()).hexdigest()}
    args.summary.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps({"summary": str(args.summary), "periods": len(rows), "cases": len(result["cases"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
