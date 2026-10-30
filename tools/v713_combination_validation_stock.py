#!/usr/bin/env python3
"""Remote-only V713 stock-sleeve sensitivity audit; never imports a broker.

This is a new reconstruction, not the unavailable Oct-11 experiment code.
Inputs remain on qmt. Only stock_summary.json is suitable for copying locally.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd


CAPITALS = (100_000, 200_000, 400_000, 1_000_000)
SCENARIOS = {"no_fees": (0.0, 0.0, False), "proportional": (0.0002, 0.0, True), "minimum_5": (0.0002, 5.0, True)}


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def previous_legal(code: str, quantity: int) -> int:
    if quantity <= 0:
        return 0
    if code.startswith("688"):
        return quantity - 1 if quantity > 200 else 0
    return max(0, quantity - 100)


def allocate(codes, prices, capital: float, budget: float | None = None):
    """Nearest legal new position; greedily trim cheapest absolute-error/value.

    Targets always use 99% of capital; code order breaks ties. This is a
    transparent NEW allocator, not claimed identical to the Oct-11 allocator.
    """
    budget = 0.99 * capital if budget is None else min(budget, 0.99 * capital)
    desired = 0.99 * capital / len(codes)
    quantities = []
    for code, price in zip(codes, prices):
        units = desired / price
        if code.startswith("688"):
            q = max(200, int(math.floor(units + 0.5)))
            if abs(desired) < abs(q * price - desired):
                q = 0
        else:
            q = int(math.floor(units / 100 + 0.5)) * 100
        quantities.append(q)
    quantities = np.array(quantities, dtype=np.int64)
    values = quantities * prices
    while values.sum() > budget + 1e-8:
        candidates = []
        for i, (code, q) in enumerate(zip(codes, quantities)):
            if q <= 0:
                continue
            newq = previous_legal(code, int(q))
            freed = (q - newq) * prices[i]
            penalty = abs(newq * prices[i] - desired) - abs(values[i] - desired)
            candidates.append((penalty / freed, str(code), i, newq))
        if not candidates:
            raise ValueError("No legal positive holdings remain while over budget")
        _, _, i, q = min(candidates)
        quantities[i] = q
        values[i] = q * prices[i]
    return quantities, values


def costs(old: dict[str, float], new: dict[str, float], date, scenario: str):
    rate, minimum, stamp = SCENARIOS[scenario]
    commission = tax = buys = sells = 0.0
    orders = 0
    stamp_rate = 0.001 if pd.Timestamp(date) < pd.Timestamp("2023-08-28") else 0.0005
    for code in sorted(set(old) | set(new)):
        delta = new.get(code, 0.0) - old.get(code, 0.0)
        if abs(delta) <= 1e-8:
            continue
        orders += 1
        commission += max(minimum, rate * abs(delta))
        buys += max(delta, 0.0)
        sells += max(-delta, 0.0)
        if stamp and delta < 0:
            tax += -delta * stamp_rate
    return {"commission": commission, "stamp_duty": tax, "total": commission + tax, "buy_value": buys, "sell_value": sells, "economic_orders": orders}


def simulate(periods: list[dict], capital: float, scenario: str):
    """Economic-position model. Adjusted returns roll values, not raw shares.

    Hence corporate actions, legal delta orders, settlement, and fills are not
    represented. This is deliberately NOT an execution or real-share ledger.
    """
    holdings: dict[str, float] = {}
    cash = float(capital)
    rows = []
    total_fee = 0.0
    for p in periods:
        starting = cash + sum(holdings.values())
        codes, prices = p["codes"], np.asarray(p["raw_prices"], dtype=float)
        budget = 0.99 * starting
        for _ in range(100):
            quantities, values = allocate(codes, prices, starting, budget)
            new = dict(zip(codes, values))
            fee = costs(holdings, new, p["start"], scenario)
            ending_cash = starting - sum(new.values()) - fee["total"]
            if ending_cash >= 0.01 * (starting - fee["total"]) - 1e-7:
                break
            budget = min(budget - 1e-6, 0.99 * (starting - fee["total"]))
        else:
            raise AssertionError("Fee/budget sizing did not converge")
        assert ending_cash >= -1e-7
        after_fee = sum(new.values()) + ending_cash
        assert abs(after_fee + fee["total"] - starting) < 1e-6
        holdings = {code: value * (1 + r) for code, value, r in zip(codes, values, p["returns"])}
        cash = ending_cash
        ending = cash + sum(holdings.values())
        total_fee += fee["total"]
        rows.append({"start": p["start"], "date": p["date"], "capital": capital, "scenario": scenario, "start_equity": starting, "end_equity": ending, "net_return": ending / starting - 1, "fee": fee["total"], "cash": cash, "held_count": int((quantities > 0).sum())})
    return rows, total_fee


def prepare(args, out: Path):
    source = Path(args.source).resolve()
    isolated = out / "source"
    isolated.mkdir(parents=True, exist_ok=False)
    files = [source / "common.py", source / "cache_meta.json"] + sorted(source.glob("*cache*.npy"))
    files += [source / "round4/v7.13/baseline.py", source / "round4/v7.13/execution.py"]
    if (source / "code_cache.txt").exists():
        files.append(source / "code_cache.txt")
    manifest = []
    for f in files:
        dest = isolated / f.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        manifest.append({"source": str(f), "copied_to": str(dest), "bytes": f.stat().st_size, "sha256": sha(f), "copy_sha256": sha(dest)})
        assert manifest[-1]["sha256"] == manifest[-1]["copy_sha256"]
    # These sources are only read by the common loader; cache output is isolated.
    for name in ("data", "v3"):
        (isolated / name).symlink_to(source / name, target_is_directory=True)
    os.environ["STRATEGY_DATA"] = args.data
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    p = isolated / "round4/v7.13/baseline.py"
    spec = importlib.util.spec_from_file_location("v713_audit_baseline", p)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    g = baseline.load_inputs(include_hydra=False)
    # The user report's endpoint is held fixed, even if source caches are newer.
    # Only stock/state columns are used; do not export baseline's empty-Hydra
    # portfolio returns as a valid V713 return series.
    g["hydra"] = pd.Series(index=pd.DatetimeIndex([]), dtype=float)
    original = baseline.run_backtest(g)
    original = original.loc[(original.date >= "2016-02-01") & (original.date <= args.end)].copy()
    return baseline, g, original, manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--end", default="2026-06-30")
    args = ap.parse_args()
    out = Path(args.output).resolve()
    if not str(out).startswith("/opt/qmt-server/private/research-runs/"):
        raise SystemExit("Outputs must be in the remote isolated research-runs directory")
    out.mkdir(parents=True, exist_ok=False)
    baseline, g, original, manifest = prepare(args, out)
    month_ends = list(g["month_ends"])
    periods, panel, missing = [], [], []
    original_by_date = original.set_index("date")
    for end in original.date:
        i = month_ends.index(end) - 1
        start = month_ends[i]
        selected = baseline.select_top50(g, i, aum=1_000_000)
        if selected is None:
            raise ValueError(f"Missing TOP50 at {start}")
        ids, weights = selected
        a, b = g["month_to_index"][start], g["month_to_index"][end]
        before = g["price"][ids, a].astype(float)
        after = g["price"][ids, b].astype(float)
        for k, ix in enumerate(ids):
            if not np.isfinite(after[k]) or after[k] <= 0:
                history = g["price"][ix, :b + 1]
                valid = np.flatnonzero(np.isfinite(history) & (history > 0))
                if not len(valid):
                    raise ValueError(f"No historical valuation for {g['codes'][ix]}")
                after[k] = history[valid[-1]]
                missing.append({"start": str(start.date()), "date": str(end.date()), "code": str(g["codes"][ix]), "last_price_date": str(g["all_dates"][valid[-1]].date())})
        raw = g["raw_price"][ids, a].astype(float)
        assert np.isfinite(raw).all() and (raw > 0).all()
        returns = after / before - 1
        p = {"start": str(start.date()), "date": str(end.date()), "codes": [str(g["codes"][j]) for j in ids], "raw_prices": raw.tolist(), "returns": returns.tolist()}
        periods.append(p)
        r = original_by_date.loc[end]
        panel.append({"start": p["start"], "date": p["date"], "known_at": p["start"], "sleeve": r.sleeve, "top50_return": float(r.top50_return), "stock_return": float(np.mean(returns)), "stock_return_99pct": 0.99 * float(np.mean(returns)), "cost": float(r.cost), "missing_holdings": sum(x["date"] == p["date"] for x in missing), "clock": "original_month_end_research_not_live_executable_time"})
    (out / "period_inputs.json").write_text(json.dumps(periods, ensure_ascii=False))
    (out / "missing_valuations.json").write_text(json.dumps(missing, ensure_ascii=False))
    pd.DataFrame(panel).to_parquet(out / "monthly_panel.parquet", index=False)
    fresh, rolling, monthly = [], [], []
    for capital in CAPITALS:
        for p in periods:
            q, values = allocate(p["codes"], np.array(p["raw_prices"]), capital)
            cash = capital - values.sum()
            assert abs(cash + values.sum() - capital) < 1e-7
            assert cash >= 0.01 * capital - 1e-7
            weights = values / capital
            target = np.full(len(q), 0.99 / len(q))
            fresh.append({"capital": capital, "start": p["start"], "date": p["date"], "weight_deviation": float(0.5 * (np.abs(weights - target).sum() + abs(cash / capital - 0.01))), "coverage": float(np.minimum(weights, target).sum() / 0.99), "held_count": int((q > 0).sum()), "cash_weight": float(cash / capital)})
        for scenario in SCENARIOS:
            path, _ = simulate(periods, capital, scenario)
            monthly.extend(path)
            for start_idx in range(len(periods) - 11):
                window = periods[start_idx:start_idx + 12]
                path, fees = simulate(window, capital, scenario)
                theory = float(np.prod([1 + np.mean(p["returns"]) for p in window]))
                theory_cash = np.array([0.99 * np.mean(p["returns"]) for p in window])
                actual = path[-1]["end_equity"] / capital
                diff = np.array([x["net_return"] for x in path]) - theory_cash
                rolling.append({"capital": capital, "scenario": scenario, "start": window[0]["start"], "date": window[-1]["date"], "relative_nav_deviation": actual / theory - 1, "monthly_annualized_tracking_error": float(np.std(diff, ddof=1) * np.sqrt(12)), "fees": fees, "missing_price_window": any(x["date"] in {p["date"] for p in window} for x in missing)})
    fresh_df, rolling_df = pd.DataFrame(fresh), pd.DataFrame(rolling)
    fresh_df.to_parquet(out / "fresh_sizing.parquet", index=False)
    rolling_df.to_parquet(out / "rolling_metrics.parquet", index=False)
    pd.DataFrame(monthly).to_parquet(out / "continuous_monthly_paths.parquet", index=False)
    summary = {
        "status": "COMPLETED_INDEPENDENT_RECONSTRUCTION_NOT_EXACT_OCT11_REPRODUCTION",
        "command": sys.argv, "script_sha256": sha(Path(__file__)), "source_files": manifest,
        "data_root": args.data, "source_release": args.source, "output_directory": str(out),
        "used_input_files": [{"path": str(f), "bytes": f.stat().st_size, "sha256": sha(f)} for f in sorted((out / "source").glob("*cache*.npy"))] + [{"path": str(baseline.common.IDX_DIR / "000852.SH.parquet"), "sha256": sha(baseline.common.IDX_DIR / "000852.SH.parquet")}],
        "source_strategy_commit_label": "88c2cb1050c7391ce84a9d524a9884dfefaf3ef4 plus ROE PIT null patch; file hashes authoritative",
        "periods": len(periods), "range": [periods[0]["start"], periods[-1]["date"]], "selection_aum": 1_000_000,
        "universe_size": len(g["codes"]), "calendar_range": [str(g["all_dates"][0].date()), str(g["all_dates"][-1].date())],
        "missing_price_months": len(set(x["date"] for x in missing)), "missing_stock_months": len(missing),
        "oct11_report_missing_months": 36, "oct11_report_missing_stock_months": 46,
        "fresh": [], "rolling": [], "remote_artifacts": [],
        "assumptions": {"cash": 0.01, "commission_rate": 0.0002, "minimum_commission_scenarios": [0, 5], "actual_broker_fees_confirmed": False, "rounding": "nearest legal initial holding; budget trim minimizes increase in absolute target-value error per released yuan; no refill", "star_lot": "688 min200 thereafter1; other symbols100, historical constant sensitivity convention", "transaction_costs": "economic holding-value changes, not actual legal share delta orders; fees deducted with >=1% postfee cash", "valuation": "adjusted-price return with last valid adjusted price for missing endpoints", "cash_weight_location": "1% inside continuous stock sleeve; not a 1% reserve on the complete V713 blend"},
        "limitations": ["Oct11 original experiment scripts/frozen input hashes/outputs not located on searched qmt paths or local code; allocator differs and no exact numerical reproduction asserted", "Current remote source caches are dated October; Oct11 frozen hashes unavailable, including ROE/shares matrices; historical input equivalence unverified", "Forward-filled prices permit valuation only, not evidence of sellability; missing holdings assumed rebalanceable", "Adjusted-return economic-value ledger does not model cash dividends, split/share events or exact legal delta orders", "No transfer fee, live slippage, order book/limit queue, suspensions at execution, T+1, broker rejection, settlement or execution capacity model", "50-name universe chosen at fixed 1M selection parameter; no claim of unbiased delisted-universe completeness", "114 overlapping annual windows are not independent samples", "No market data downloaded locally; no production writes, orders, or deployment", "Month-end model uses same-close sizing; signal publication and real execution timing are not reconstructed"],
        "assertions": {"fresh_cases": len(fresh), "all_fresh_capital_conserved": True, "all_fresh_cash_at_least_1pct": True, "all_paths_nonnegative_cash_and_fee_conservation": True},
    }
    for capital, frame in fresh_df.groupby("capital"):
        summary["fresh"].append({"capital": int(capital), "cases": len(frame), "weight_deviation_median": float(frame.weight_deviation.median()), "weight_deviation_p90": float(frame.weight_deviation.quantile(0.9)), "coverage_median": float(frame.coverage.median()), "held_50_count": int((frame.held_count == 50).sum()), "minimum_held_count": int(frame.held_count.min())})
    for (capital, scenario), frame in rolling_df.groupby(["capital", "scenario"]):
        summary["rolling"].append({"capital": int(capital), "scenario": scenario, "windows": len(frame), "relative_nav_deviation_median": float(frame.relative_nav_deviation.median()), "relative_nav_deviation_worst": float(frame.relative_nav_deviation.min()), "tracking_error_median": float(frame.monthly_annualized_tracking_error.median()), "clean_windows": int((~frame.missing_price_window).sum())})
    for f in sorted(out.glob("*.parquet")):
        summary["remote_artifacts"].append({"path": str(f), "sha256": sha(f), "bytes": f.stat().st_size})
    summary["original_sources_unchanged_after_run"] = all(sha(Path(x["source"])) == x["sha256"] for x in manifest)
    if not summary["original_sources_unchanged_after_run"]:
        raise AssertionError("Original source/cache changed during run")
    (out / "stock_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps({"output": str(out / "stock_summary.json"), "periods": len(periods), "missing_months": summary["missing_price_months"], "fresh": summary["fresh"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
