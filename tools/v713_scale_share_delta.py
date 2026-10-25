#!/usr/bin/env python3
"""Remote-only V713 stock-sleeve scale audit with share-count fees and a no-trade band.

The archived Oct-16 model charges a commission whenever a holding's economic
value differs from its new target value, so a holding whose share count did not
change still pays the minimum fee. This audit carries integer shares between
month-end rebalances, charges only changed share counts, and optionally keeps a
holding whose value is within a band of its equal-weight target. Targets,
returns and fee formulas are the archived ones. Inputs remain on qmt.
"""
from __future__ import annotations

import decimal
import hashlib
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("archived_stock_model", HERE / "v713_combination_validation_stock.py")
stock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stock)

# Below this value / (shares * price) ratio the share count is unchanged and the
# residual is cash (dividends, rounded adjusted prices); above it the period
# contained bonus or split shares.
SPLIT_FACTOR = 1.095

# Stock-account fees confirmed on 2026-10-25: commission 0.854bp with a 5-yuan
# minimum on commission alone; stamp duty on sells and the statutory transfer fee
# on both sides are charged on top.
CONFIRMED = "confirmed_20261025"
CONFIRMED_COMMISSION = 0.0000854

ROOT = Path("/opt/qmt-server/private/research-runs")
SOURCE = ROOT / "v713-combination-validation-20261016"
PRIOR = ROOT / "v713-scale-mapping-20261019-01"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def carry(shares: int, value: float, price: float) -> tuple[int, float]:
    """Share count and cash residual after one period; cash dividends stay cash."""
    factor = value / (shares * price)
    held = shares if factor < SPLIT_FACTOR else int(math.floor(shares * factor + 1e-9))
    return held, value - held * price


def confirmed_costs(old: dict[str, float], new: dict[str, float], date) -> dict:
    day = pd.Timestamp(date)
    stamp_rate = 0.001 if day < pd.Timestamp("2023-08-28") else 0.0005
    transfer_rate = 0.00002 if day < pd.Timestamp("2022-04-29") else 0.00001
    commission = tax = transfer = buys = sells = 0.0
    orders = 0
    for code in sorted(set(old) | set(new)):
        delta = new.get(code, 0.0) - old.get(code, 0.0)
        if abs(delta) <= 1e-8:
            continue
        orders += 1
        commission += max(5.0, CONFIRMED_COMMISSION * abs(delta))
        transfer += transfer_rate * abs(delta)
        buys += max(delta, 0.0)
        sells += max(-delta, 0.0)
        if delta < 0:
            tax += -delta * stamp_rate
    return {"commission": commission, "stamp_duty": tax, "transfer_fee": transfer,
            "total": commission + tax + transfer, "buy_value": buys, "sell_value": sells, "economic_orders": orders}


def costs(old, new, date, scenario: str) -> dict:
    return confirmed_costs(old, new, date) if scenario == CONFIRMED else stock.costs(old, new, date, scenario)


def rebalance(codes, prices, held: dict[str, int], exiting: dict[str, float], equity: float, band: float, scenario: str, date):
    """Final share counts and fees for one month-end rebalance.

    A held name keeps its shares when the archived target equals them or when
    its value is within band * equal-weight target value. The archived greedy
    trim then restores the >=1% post-fee cash reserve, kept names included.
    """
    prices = np.asarray(prices, dtype=float)
    target, _ = stock.allocate(codes, prices, equity)
    desired = 0.99 * equity / len(codes)
    q = target.copy()
    for i, code in enumerate(codes):
        h = held.get(code, 0)
        if h > 0 and (target[i] == h or abs(h * prices[i] - desired) <= band * desired):
            q[i] = h
    old = {code: held[code] * price for code, price in zip(codes, prices) if held.get(code, 0) > 0}
    old.update(exiting)
    while True:
        fee = costs(old, {code: n * price for code, n, price in zip(codes, q, prices) if n > 0}, date, scenario)
        if equity - (q * prices).sum() - fee["total"] >= 0.01 * (equity - fee["total"]) - 1e-7:
            return q, fee
        candidates = []
        for i, (code, n) in enumerate(zip(codes, q)):
            if n <= 0:
                continue
            newq = stock.previous_legal(code, int(n))
            freed = (n - newq) * prices[i]
            penalty = abs(newq * prices[i] - desired) - abs(n * prices[i] - desired)
            candidates.append((penalty / freed, str(code), i, newq))
        if not candidates:
            raise ValueError("No legal positive holdings remain while short of the cash reserve")
        _, _, i, newq = min(candidates)
        q[i] = newq


def simulate_shares(periods: list[dict], capital: float, scenario: str, band: float):
    """Integer-share path on the archived economic returns and targets."""
    shares: dict[str, int] = {}
    values: dict[str, float] = {}
    cash = float(capital)
    rows = []
    total_fee = 0.0
    for p in periods:
        codes, prices = p["codes"], np.asarray(p["raw_prices"], dtype=float)
        price_of = dict(zip(codes, prices))
        held, exiting = {}, {}
        for code, value in values.items():
            if code in price_of:
                held[code], residual = carry(shares[code], value, price_of[code])
                cash += residual
            else:
                exiting[code] = value
        equity = cash + sum(n * price_of[code] for code, n in held.items()) + sum(exiting.values())
        q, fee = rebalance(codes, prices, held, exiting, equity, band, scenario, p["start"])
        cash = equity - float((q * prices).sum()) - fee["total"]
        assert cash >= -1e-7
        after_fee = equity - fee["total"]
        weights = q * prices / after_fee
        deviation = 0.5 * (np.abs(weights - 0.99 / len(codes)).sum() + abs(cash / after_fee - 0.01))
        shares = {code: int(n) for code, n in zip(codes, q) if n > 0}
        values = {code: n * price * (1 + r) for code, n, price, r in zip(codes, q, prices, p["returns"]) if n > 0}
        ending = cash + sum(values.values())
        total_fee += fee["total"]
        rows.append({"start": p["start"], "date": p["date"], "capital": capital, "scenario": scenario, "band": band, "start_equity": equity, "end_equity": ending, "net_return": ending / equity - 1, "fee": fee["total"], "orders": fee["economic_orders"], "cash": cash, "held_count": int((q > 0).sum()), "weight_deviation": float(deviation)})
    return rows, total_fee


def archived_path(periods: list[dict], capital: float, scenario: str):
    """Archived stock.simulate, also recording its accepted order count per month."""
    calls = {}
    original = stock.costs

    def recording(old, new, date, scenario):
        result = original(old, new, date, scenario)
        calls[str(date)] = result["economic_orders"]
        return result

    stock.costs = recording
    try:
        rows, fees = stock.simulate(periods, capital, scenario)
    finally:
        stock.costs = original
    for row in rows:
        row["orders"] = calls[row["start"]]
    return rows, fees


def startup_check(out: Path, protocol: dict) -> dict:
    """Fail before any result unless the run directory and frozen inputs are the archived ones."""
    if not str(out).startswith(str(ROOT) + "/") or out == ROOT:
        raise SystemExit("Run only from a new directory under " + str(ROOT))
    if (out / "share_delta_summary.json").exists():
        raise SystemExit("Outputs already exist; use a new run directory")
    archived = json.loads((SOURCE / "stock" / "stock_summary.json").read_text())
    prior = json.loads((PRIOR / "scale_summary.json").read_text())
    checks = {
        "sibling stock model is the archived script": sha(HERE / "v713_combination_validation_stock.py") == archived["script_sha256"],
        "archived script copy unchanged": sha(SOURCE / "v713_combination_validation_stock.py") == archived["script_sha256"],
        "period inputs match the Oct-19 manifest": sha(SOURCE / "stock" / "period_inputs.json") == prior["source_manifest"][str(SOURCE / "stock" / "period_inputs.json")],
        "rolling metrics match the archived artifact": any(Path(a["path"]).name == "rolling_metrics.parquet" and sha(Path(a["path"])) == a["sha256"] for a in archived["remote_artifacts"]),
        "protocol fixes bands before results": bool(protocol["bands"]) and protocol["bands"][0] == 0.0,
    }
    if not all(checks.values()):
        raise SystemExit("Startup check failed: " + json.dumps(checks, ensure_ascii=False))
    return {"checks": checks, "prior": prior}


def stats(values) -> dict:
    a = np.asarray(values, dtype=float)
    return {"n": len(a), "min": float(a.min()), "p05": float(np.quantile(a, .05)), "median": float(np.median(a)), "p95": float(np.quantile(a, .95)), "max": float(a.max())}


def main():
    out = HERE
    protocol = json.loads((out / "protocol.json").read_text(encoding="utf-8"))
    start = startup_check(out, protocol)
    qa = [{"name": k, "passed": v} for k, v in start["checks"].items()]

    def check(name, condition, detail=None):
        qa.append({"name": name, "passed": bool(condition), "detail": detail})

    periods = json.loads((SOURCE / "stock" / "period_inputs.json").read_text())
    missing = {x["date"] for x in json.loads((SOURCE / "stock" / "missing_valuations.json").read_text())}
    archived_metrics = pd.read_parquet(SOURCE / "stock" / "rolling_metrics.parquet").set_index(["capital", "scenario", "start", "date"])
    windows = [periods[i:i + 12] for i in range(len(periods) - 11)]
    check("114 rolling windows", len(windows) == 114)
    capitals, scenarios = protocol["capitals"], protocol["scenarios"]

    def variants(scenario):
        # The archived value-delta model only knows its own fee scenarios.
        archived = [("archived_value_delta", None)] if scenario in stock.SCENARIOS else []
        return archived + [(f"share_delta_band_{b:.2f}", b) for b in protocol["bands"]]

    rows = []
    replay_error = no_fee_error = 0.0
    for window in windows:
        key = (window[0]["start"], window[-1]["date"])
        theory = float(np.prod([1 + np.mean(p["returns"]) for p in window]))
        theory_cash = np.array([0.99 * np.mean(p["returns"]) for p in window])
        flagged = any(p["date"] in missing for p in window)
        for capital in capitals:
            # Share ledger with band 0 must equal the archived path when fees are zero.
            a, _ = stock.simulate(window, capital, "no_fees")
            b, _ = simulate_shares(window, capital, "no_fees", 0.0)
            no_fee_error = max(no_fee_error, abs(b[-1]["end_equity"] / a[-1]["end_equity"] - 1))
            for scenario in scenarios:
                for variant, band in variants(scenario):
                    path, fees = archived_path(window, capital, scenario) if band is None else simulate_shares(window, capital, scenario, band)
                    factor = path[-1]["end_equity"] / capital
                    if band is None:
                        replay_error = max(replay_error, abs(factor / theory - 1 - archived_metrics.loc[(capital, scenario) + key, "relative_nav_deviation"]))
                    diff = np.array([x["net_return"] for x in path]) - theory_cash
                    rows.append({"variant": variant, "band": band, "capital": capital, "scenario": scenario, "start": key[0], "end": key[1],
                                 "missing_price_window": flagged, "factor": factor, "relative_nav_deviation": factor / theory - 1,
                                 "tracking_error": float(np.std(diff, ddof=1) * np.sqrt(12)), "fees": fees,
                                 "orders_per_month": float(np.mean([x["orders"] for x in path])),
                                 "weight_deviation": float(np.median([x["weight_deviation"] for x in path])) if band is not None else None})
    check("archived value-delta paths reproduce Oct-16 rolling metrics", replay_error < 1e-10, replay_error)
    check("share ledger at band 0 equals archived path with no fees", no_fee_error < 1e-9, no_fee_error)
    frame = pd.DataFrame(rows)
    frame.to_parquet(out / "share_delta_windows_private.parquet", index=False)

    small, large = protocol["primary_pair"]
    legs, pairs = [], []
    for (variant, scenario), g in frame.groupby(["variant", "scenario"], sort=False):
        for capital in (small, large):
            c = g[g.capital == capital]
            legs.append({"variant": variant, "scenario": scenario, "capital": capital, "windows": len(c),
                         "relative_nav_deviation": stats(c.relative_nav_deviation), "tracking_error_median": float(c.tracking_error.median()),
                         "orders_per_month_median": float(c.orders_per_month.median()), "fees_median": float(c.fees.median()),
                         "weight_deviation_median": None if c.weight_deviation.isna().all() else float(c.weight_deviation.median())})
        s = g[g.capital == small].set_index(["start", "end"])
        l = g[g.capital == large].set_index(["start", "end"])
        joined = s.join(l, lsuffix="_small", rsuffix="_large", how="inner")
        check(f"114 matched windows {variant} {scenario}", len(joined) == 114)
        gap = joined.factor_large / joined.factor_small - 1
        decimal_gap = [float(decimal.Decimal(repr(fl)) / decimal.Decimal(repr(fs)) - 1) for fs, fl in zip(joined.factor_small, joined.factor_large)]
        check(f"Decimal ratio {variant} {scenario}", np.max(np.abs(gap.to_numpy() - decimal_gap)) < 1e-12)
        flagged = joined.missing_price_window_small | joined.missing_price_window_large
        for population, use in (("all", gap), ("clean", gap[~flagged])):
            pairs.append({"variant": variant, "scenario": scenario, "population": population, "relative_terminal_gap": stats(use),
                          "large_outperforms_fraction": float((use > 0).mean()),
                          "cash_difference_median": float(np.median(large * (joined.factor_large - joined.factor_small)[use.index]))})
    prior_primary = next(x for x in start["prior"]["comparisons"] if x["small"] == small and x["large"] == large and x["scenario"] == "minimum_5" and x["population"] == "all")
    ours = next(x for x in pairs if x["variant"] == "archived_value_delta" and x["scenario"] == "minimum_5" and x["population"] == "all")
    check("archived minimum-5 paired median equals the Oct-19 result", abs(ours["relative_terminal_gap"]["median"] - prior_primary["relative_terminal_gap"]["median"]) < 1e-12,
          [ours["relative_terminal_gap"]["median"], prior_primary["relative_terminal_gap"]["median"]])
    summary = {"experiment_id": protocol["experiment_id"], "status": "COMPLETED_FEE_COUNTING_SENSITIVITY_NOT_EXECUTION_CERTIFICATION",
               "formula": "large_end / (large_start/small_start * small_end) - 1", "pairs": pairs, "legs": legs,
               "qa_count": len(qa), "qa_all_passed": all(q["passed"] for q in qa), "limits": protocol["limits"],
               "production_modified": False, "orders_submitted": 0}
    (out / "verification.json").write_text(json.dumps({"checks": qa, "all_passed": summary["qa_all_passed"]}, indent=2, ensure_ascii=False))
    (out / "share_delta_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    assert summary["qa_all_passed"], [q for q in qa if not q["passed"]]
    primary = [x for x in pairs if x["scenario"] == "minimum_5" and x["population"] == "all"]
    print(json.dumps([{"variant": x["variant"], "median": x["relative_terminal_gap"]["median"]} for x in primary]))


if __name__ == "__main__":
    main()
