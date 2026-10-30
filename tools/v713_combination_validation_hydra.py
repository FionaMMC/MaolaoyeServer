"""Align existing remote Hydra research proxies with one frozen V713 stock panel.

Run on qmt only. Production sources are read-only; period rows stay in an
independent research-runs directory. This is not a broker or trading client.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import pandas as pd


RESEARCH_ROOT = Path("/opt/qmt-server/private/research-runs")
HYDRA_ROOT = Path("/opt/qmt-refresh/releases/permenant_portfolio-66985a1/v48")
ORIGINAL = HYDRA_ROOT / "output_forward_rate_bond/daily_returns.parquet"
RB_LEDGER = HYDRA_ROOT / "execution_ledgers/v48.1-RB-66985a1"
RB = RB_LEDGER / "daily_returns.parquet"
SERVER = Path("/opt/qmt-server/v2.3/server")
STOCK_WEIGHT = {"TOP50": 1.0, "T1_5050": 0.5, "AUX_HYDRA": 0.0}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact(path: Path) -> dict:
    return {"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size}


def daily_returns(path: Path) -> pd.Series:
    frame = pd.read_parquet(path)
    series = frame.set_index("date")["ret"] if "date" in frame else frame["ret"]
    series.index = pd.to_datetime(series.index).normalize()
    series = series.astype(float).sort_index()
    if not series.index.is_unique or series.empty or not series.map(math.isfinite).all():
        raise ValueError(f"Invalid daily return history: {path}")
    if (series <= -1).any():
        raise ValueError(f"Invalid return <= -100%: {path}")
    return series


def history_metadata(path: Path, returns: pd.Series) -> dict:
    return {**artifact(path), "rows": len(returns),
            "start": returns.index.min().date().isoformat(),
            "end": returns.index.max().date().isoformat()}


def aligned_periods(stock: pd.DataFrame, original: pd.Series, rb: pd.Series, calendar=None):
    """Never silently replace a missing period or daily observation with zero."""
    rows, excluded = [], []
    for source in stock.to_dict("records"):
        start = pd.Timestamp(source["start"])
        end = pd.Timestamp(source.get("end", source.get("date")))
        known = pd.Timestamp(source.get("known_at", start))
        state = str(source["sleeve"])
        if not known <= start < end or state not in STOCK_WEIGHT:
            raise ValueError("Invalid stock period or signal timestamp")
        if any(value != value.normalize() for value in (start, end, known)):
            raise ValueError("Monthly research periods require date-only boundaries and signals; intraday times cannot be discarded")
        a = original.loc[(original.index > start) & (original.index <= end)]
        b = rb.loc[(rb.index > start) & (rb.index <= end)]
        reason = None
        if any(day not in history.index for day in (start, end) for history in (original, rb)):
            reason = "boundary_outside_observed_daily_calendar"
        elif a.empty or b.empty or not a.index.equals(b.index):
            reason = "daily_calendar_mismatch_or_empty"
        elif calendar is not None and not a.index.equals(calendar[(calendar > start) & (calendar <= end)]):
            reason = "shared_returns_incomplete_against_stock_calendar"
        elif a.isna().any() or b.isna().any():
            reason = "nonfinite_daily_return"
        if reason:
            excluded.append({"start": start.date().isoformat(), "end": end.date().isoformat(), "reason": reason})
            continue
        gross_key = next((key for key in ("top50_return", "stock_gross_return", "stock_return") if key in source), None)
        if gross_key is None:
            raise ValueError("Stock panel must include an explicit stock gross return")
        gross = float(source[gross_key])
        cost = float(source.get("cost", 0.0))
        # Keep the original gross baseline intact. Net security-leg treatment
        # is an explicit alternate proxy, not a claim about actual costs.
        net = gross - cost
        hydro = float((1 + a).prod() - 1)
        rb_hydro = float((1 + b).prod() - 1)
        if not all(math.isfinite(value) for value in (gross, cost, net, hydro, rb_hydro)):
            raise ValueError("Nonfinite stock or compounded return")
        w = STOCK_WEIGHT[state]
        row = {"start": start.date().isoformat(), "end": end.date().isoformat(),
               "known_at": known.date().isoformat(), "sleeve": state,
               "stock_return": gross, "stock_gross_return": gross, "stock_net_proxy_return": net, "cost": cost,
               "hydra_return": hydro, "rb_hydra_return": rb_hydro,
               "observed_daily_rows": len(a),
               "original_baseline_gross_return": w * gross + (1 - w) * hydro,
               "original_baseline_net_return": w * gross + (1 - w) * hydro - cost,
               "unit_leg_original_return": w * gross + (1 - w) * hydro,
               "unit_leg_rb_return": w * gross + (1 - w) * rb_hydro}
        if "stock_return" in source:
            row["stock_carry_valuation_return"] = float(source["stock_return"])
        # Optional explicitly net size-sensitive leg returns supplied by the
        # stock audit remain intact; the original baseline is never overwritten.
        for name in ("stock_100k_return", "stock_200k_return", "stock_400k_return"):
            if name in source and pd.notna(source[name]):
                row[name] = float(source[name])
        rows.append(row)
    rows.sort(key=lambda row: row["start"])
    for previous, current in zip(rows, rows[1:]):
        if current["start"] != previous["end"]:
            raise ValueError("Complete common observations contain an internal gap; do not bridge it")
    return rows, excluded


def target_metadata() -> dict:
    """Export target identities and NAV coverage only, never values/positions."""
    target = SERVER / "plugins/v713/data/v713_target_latest.parquet"
    child = HYDRA_ROOT / "output_forward_rate_bond/v713_hydra_latest.parquet"
    result = {"observed_at_utc": datetime.now(timezone.utc).isoformat()}
    safe_keys = {"strategy_version", "as_of_date", "decision_date", "sleeve", "basket_sha256",
                 "hydra_weights_sha256", "publisher_source_commit", "generated_at_utc",
                 "signal_anchor_date", "selected_duration_bucket", "selected_duration_etf",
                 "training_label_end", "asset_weights_sha256", "signals_sha256",
                 "training_audit_sha256"}
    for label, path in (("v713_main", target), ("v713_hydra_child", child)):
        data = {**artifact(path)}
        sidecar = path.with_suffix(".json")
        if sidecar.exists():
            content = json.loads(sidecar.read_text())
            data["sidecar"] = {**artifact(sidecar), "metadata": {key: content[key] for key in safe_keys if key in content}}
        result[label] = data
    database = SERVER / "pipeline-server.db"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    try:
        row = connection.execute(
            "SELECT signal_date, source_sha256, created_at, weights FROM oms_target_versions "
            "WHERE instance_id=? ORDER BY signal_date DESC, created_at DESC LIMIT 1",
            ("live_hydra_v481_rb",),
        ).fetchone()
        if row:
            weights = json.loads(row[3])
            result["independent_hydra_oms"] = {
                "instance_id": "live_hydra_v481_rb", "signal_date": row[0], "source_sha256": row[1],
                "created_at": row[2], "etf_symbols": sorted(weights),
                "model_commit_authenticated_from_target": False,
            }
        else:
            result["independent_hydra_oms"] = {"present": False}
        result["perf_snapshot_coverage_only"] = [
            {"instance_id": row[0], "execution_domain": row[1], "rows": row[2], "start": row[3], "end": row[4]}
            for row in connection.execute(
                "SELECT instance_id, execution_domain, COUNT(*), MIN(date), MAX(date) FROM perf_snapshots "
                "WHERE instance_id LIKE '%v713%' OR instance_id='live_hydra_v481_rb' "
                "GROUP BY instance_id, execution_domain ORDER BY instance_id, execution_domain"
            )
        ]
        result["instance_ledger_classification_only"] = [
            {"instance_id": row[0], "execution_domain": row[1], "ledger_mode": row[2]}
            for row in connection.execute(
                "SELECT instance_id, execution_domain, ledger_mode FROM instance_state "
                "WHERE instance_id LIKE '%v713%' OR instance_id='live_hydra_v481_rb' ORDER BY instance_id"
            )
        ]
        result["v713_live_attributed_instance_count"] = connection.execute(
            "SELECT COUNT(*) FROM instance_state WHERE instance_id LIKE '%v713%' "
            "AND execution_domain='live' AND ledger_mode='attributed'"
        ).fetchone()[0]
        result["v713_live_snapshot_rows"] = connection.execute(
            "SELECT COUNT(*) FROM perf_snapshots WHERE instance_id LIKE '%v713%' AND execution_domain='live'"
        ).fetchone()[0]
        result["v713_oms_target_rows"] = connection.execute(
            "SELECT COUNT(*) FROM oms_target_versions WHERE instance_id LIKE '%v713%'"
        ).fetchone()[0]
        result["coverage_name_scope"] = "V713 instances whose server instance_id contains v713; unrelated external accounts are not surveyed"
        result["performance_coverage_caution"] = (
            "Snapshot existence is not authenticated broker NAV. No account values, cash flows, "
            "holdings, or live NAV series were read. No actual two-leg performance is inferred."
        )
    finally:
        connection.rollback()
        connection.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-panel", required=True, type=Path)
    parser.add_argument("--trading-calendar", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    out = args.output_dir.resolve()
    if not out.is_relative_to(RESEARCH_ROOT.resolve()):
        raise ValueError("All period details must remain in remote research-runs")
    if args.stock_panel.suffix == ".json":
        payload = json.loads(args.stock_panel.read_text())
        stock = pd.DataFrame(payload["rows"] if isinstance(payload, dict) else payload)
    elif args.stock_panel.suffix == ".parquet":
        stock = pd.read_parquet(args.stock_panel)
    else:
        stock = pd.read_csv(args.stock_panel)
    original, rb = daily_returns(ORIGINAL), daily_returns(RB)
    calendar_payload = json.loads(args.trading_calendar.read_text())
    dates = calendar_payload["dates"] if isinstance(calendar_payload, dict) else calendar_payload
    calendar = pd.DatetimeIndex(pd.to_datetime(dates)).normalize().sort_values()
    if not calendar.is_unique or calendar.empty:
        raise ValueError("Expected stock trading calendar must be nonempty and unique")
    rows, excluded = aligned_periods(stock, original, rb, calendar)
    if not rows:
        raise ValueError("No complete common research months")
    manifest = json.loads((RB_LEDGER / "manifest.json").read_text())
    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "script": artifact(Path(__file__)),
        "stock_panel": artifact(args.stock_panel), "stock_trading_calendar": artifact(args.trading_calendar),
        "original_forward_rate_proxy": history_metadata(ORIGINAL, original),
        "rb_66985_research_ledger": history_metadata(RB, rb), "rb_manifest": artifact(RB_LEDGER / "manifest.json"),
        "rb_execution_assumptions": {key: manifest[key] for key in (
            "strategy_version", "status", "execution_rule", "price_source", "dividend_source", "lot_rounding",
            "buy_cost_bps", "sell_cost_bps", "weight_source_sha256") if key in manifest},
        "base_weights": artifact(HYDRA_ROOT / "output/asset_weights.parquet"),
        "rb_weights": artifact(HYDRA_ROOT / "output_v481_rb/asset_weights.parquet"),
        "research_only": True, "actual_live_nav": False, "periods_per_year": 12,
        "comparison_basis": "Identical stock returns, original states and complete daily calendars; different Hydra research proxies",
        "known_at_semantics": "Research month-end information assumption, not proof of executable closing-price orders",
        "stock_return_semantics": "Original gross TOP50 return; stock_net_proxy_return separately subtracts original stock turnover cost; no lot execution added",
        "stock_carry_valuation_return_semantics": "Separate stock audit sensitivity preserving held names and carrying prior adjusted marks when a period-end quote is missing",
        "baseline_cost_difference": "Original baseline subtracts stock turnover cost even in AUX_HYDRA; unit-leg combination scales stock cost by stock sleeve weight",
        "original_hydra_caveat": "Synthetic bond-index monthly returns spread over daily returns; not an executable ETF account NAV",
        "rb_caveat": "Old 66985 research ledger, not authenticated current aa6 live model or broker NAV; fractional positions and next-open assumptions",
        "interpretation": "Demonstrates non-equivalence, not an isolated causal RB effect or a strategy-selection performance contest",
        "coverage_rule": "Both period boundaries observed in both series; daily dates in (start,end] equal each other and the independently exported stock calendar; no zero filling; internal gaps rejected",
    }
    leg_differences = [row["rb_hydra_return"] - row["hydra_return"] for row in rows]
    combination_differences = [row["unit_leg_rb_return"] - row["unit_leg_original_return"] for row in rows]
    summary = {
        "metadata": metadata, "current_targets": target_metadata(),
        "common_months": len(rows), "start": rows[0]["start"], "end": rows[-1]["end"],
        "state_counts": dict(Counter(row["sleeve"] for row in rows)),
        "stock_input_months": len(stock), "excluded_months": len(excluded),
        "exclusion_reasons": dict(Counter(row["reason"] for row in excluded)),
        "hydra_return_nonidentical_months": sum(abs(value) > 1e-12 for value in leg_differences),
        "hydra_mean_abs_monthly_difference_bps": sum(abs(value) for value in leg_differences) / len(rows) * 10000,
        "hydra_max_abs_monthly_difference_bps": max(abs(value) for value in leg_differences) * 10000,
        "same_state_combination_nonidentical_months": sum(abs(value) > 1e-12 for value in combination_differences),
        "same_state_combination_max_abs_monthly_difference_bps": max(abs(value) for value in combination_differences) * 10000,
        "remote_period_panel": str(out / "monthly_panel.json"),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "monthly_panel.json").write_text(json.dumps({"metadata": metadata, "rows": rows}, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    (out / "excluded_periods.json").write_text(json.dumps(excluded, indent=2) + "\n")
    (out / "hydra_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
