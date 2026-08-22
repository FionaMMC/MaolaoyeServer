#!/usr/bin/env python3
"""Server-side EOD market fallback for a missing Windows/QMT upload.

The QMT store remains the preferred source.  This command exits without writing
when the CSI1000 probe already contains the requested day.  Otherwise it fetches
bulk unadjusted bars from Tushare, validates coverage and values for every store
category, and only then atomically appends bars to existing parquet files.

It deliberately updates existing symbols only.  Universe membership and new
instrument discovery remain owned by the normal QMT ingestion path.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Callable

import pandas as pd
import requests


API_URL = "http://api.tushare.pro"
FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"
PROBE_CATEGORY = "indexes"
PROBE_SYMBOL = "000852.SH"
MIN_COVERAGE = {"stocks": 0.98, "etfs": 0.98, "indexes": 1.0}
CATEGORY_API = {"stocks": "daily", "etfs": "fund_daily", "indexes": "index_daily"}


@dataclass(frozen=True)
class PreparedCategory:
    category: str
    api_name: str
    bars: pd.DataFrame
    existing_count: int
    matched_count: int

    @property
    def coverage(self) -> float:
        return self.matched_count / self.existing_count if self.existing_count else 0.0


def _half_up(value: object, places: int = 0) -> float | int:
    quantum = Decimal(1).scaleb(-places)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    return int(rounded) if places == 0 else float(rounded)


def _call_tushare(api_name: str, trade_date: str, token: str) -> pd.DataFrame:
    payload = {
        "api_name": api_name,
        "token": token,
        "params": {"trade_date": trade_date},
        "fields": FIELDS,
    }
    for attempt in range(3):
        try:
            response = requests.post(API_URL, json=payload, timeout=120)
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                raise RuntimeError(f"{api_name} failed: {body.get('msg')}")
            data = body.get("data") or {}
            return pd.DataFrame(data.get("items") or [], columns=data.get("fields") or [])
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
    raise AssertionError("unreachable")


def _latest_open_trade_day(token: str) -> str:
    today = datetime.now().strftime("%Y%m%d")
    start = (pd.Timestamp(today) - pd.Timedelta(days=20)).strftime("%Y%m%d")
    payload = {
        "api_name": "trade_cal",
        "token": token,
        "params": {
            "exchange": "SSE", "start_date": start, "end_date": today, "is_open": "1",
        },
        "fields": "cal_date",
    }
    response = requests.post(API_URL, json=payload, timeout=60)
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(f"trade_cal failed: {body.get('msg')}")
    items = (body.get("data") or {}).get("items") or []
    if not items:
        raise RuntimeError("trade_cal returned no open day")
    return max(str(row[0]) for row in items)


def _file_max(path: Path) -> str | None:
    if not path.exists():
        return None
    values = pd.read_parquet(path, columns=["trade_date"])["trade_date"]
    return str(int(pd.to_numeric(values, errors="raise").max())) if len(values) else None


def startup_check(store: Path, trade_date: str, token: str) -> None:
    """Validate all prerequisites before any network call or parquet write."""
    if len(trade_date) != 8 or not trade_date.isdigit():
        raise RuntimeError(f"invalid trade_date: {trade_date!r}")
    if not token.strip():
        raise RuntimeError("TUSHARE_TOKEN is not configured")
    if not store.is_dir():
        raise RuntimeError(f"market store does not exist: {store}")
    probe = store / PROBE_CATEGORY / f"{PROBE_SYMBOL}.parquet"
    if not probe.is_file():
        raise RuntimeError(f"market freshness probe does not exist: {probe}")


def _normalize(raw: pd.DataFrame, trade_date: str, existing: set[str]) -> pd.DataFrame:
    required = set(FIELDS.split(","))
    missing = required - set(raw.columns)
    if missing:
        raise RuntimeError(f"Tushare response missing columns: {sorted(missing)}")
    bars = raw[raw["ts_code"].astype(str).isin(existing)].copy()
    bars["ts_code"] = bars["ts_code"].astype(str)
    bars["trade_date"] = bars["trade_date"].astype(str)
    if set(bars["trade_date"]) - {trade_date}:
        raise RuntimeError("Tushare response contains an unexpected trade_date")
    if bars["ts_code"].duplicated().any():
        raise RuntimeError("Tushare response contains duplicate symbols")

    numeric = ["open", "high", "low", "close", "vol", "amount"]
    bars[numeric] = bars[numeric].apply(pd.to_numeric, errors="raise")
    if bars[numeric].isna().any().any():
        raise RuntimeError("Tushare response contains null numeric values")
    if ((bars[["open", "high", "low", "close"]] <= 0).any().any()
            or (bars[["vol", "amount"]] < 0).any().any()):
        raise RuntimeError("Tushare response contains invalid price/volume values")
    if ((bars["high"] < bars[["open", "close", "low"]].max(axis=1)).any()
            or (bars["low"] > bars[["open", "close", "high"]].min(axis=1)).any()):
        raise RuntimeError("Tushare OHLC relationship is invalid")

    for column in ("open", "high", "low", "close"):
        bars[column] = bars[column].map(lambda value: _half_up(value, 3))
    bars["volume"] = bars["vol"].map(_half_up)
    bars["amount"] = bars["amount"].map(lambda value: float(_half_up(float(value) * 1000)))
    bars["suspendFlag"] = 0
    return bars.set_index("ts_code")[[
        "trade_date", "open", "high", "low", "close",
        "volume", "amount", "suspendFlag",
    ]]


def prepare(
    store: Path,
    trade_date: str,
    token: str,
    fetcher: Callable[[str, str, str], pd.DataFrame] = _call_tushare,
) -> list[PreparedCategory]:
    prepared: list[PreparedCategory] = []
    for category, api_name in CATEGORY_API.items():
        existing = {path.stem for path in (store / category).glob("*.parquet")}
        if not existing:
            raise RuntimeError(f"market store category is empty: {category}")
        bars = _normalize(fetcher(api_name, trade_date, token), trade_date, existing)
        item = PreparedCategory(category, api_name, bars, len(existing), len(bars))
        if item.coverage < MIN_COVERAGE[category]:
            raise RuntimeError(
                f"{category} coverage {item.coverage:.4%} below "
                f"{MIN_COVERAGE[category]:.2%} ({item.matched_count}/{item.existing_count})"
            )
        prepared.append(item)
    return prepared


def _atomic_append(path: Path, bar: pd.Series) -> None:
    old = pd.read_parquet(path)
    columns = list(old.columns)
    row = pd.DataFrame([{column: bar.get(column, 0) for column in columns}])
    for column, dtype in old.dtypes.items():
        row[column] = row[column].astype(dtype)
    out = pd.concat([old[old["trade_date"].astype(str) != str(bar["trade_date"])], row],
                    ignore_index=True)
    out = out.sort_values("trade_date").reset_index(drop=True)
    for column, dtype in old.dtypes.items():
        out[column] = out[column].astype(dtype)
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False,
    ) as handle:
        tmp = Path(handle.name)
    try:
        out.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def run_fallback(
    store: Path,
    trade_date: str,
    token: str,
    *,
    dry_run: bool = False,
    force: bool = False,
    fetcher: Callable[[str, str, str], pd.DataFrame] = _call_tushare,
) -> dict:
    startup_check(store, trade_date, token)
    probe = store / PROBE_CATEGORY / f"{PROBE_SYMBOL}.parquet"
    probe_max = _file_max(probe)
    if not force and probe_max is not None and probe_max >= trade_date:
        return {"trade_date": trade_date, "skipped": "qmt_store_already_fresh", "probe_max": probe_max}

    prepared = prepare(store, trade_date, token, fetcher)
    summary = {
        "trade_date": trade_date,
        "source": "tushare_eod_fallback",
        "dry_run": dry_run,
        "categories": {
            item.category: {
                "api": item.api_name,
                "existing": item.existing_count,
                "matched": item.matched_count,
                "coverage": round(item.coverage, 6),
            }
            for item in prepared
        },
    }
    if dry_run:
        return summary

    for item in prepared:
        root = store / item.category
        for symbol, bar in item.bars.iterrows():
            _atomic_append(root / f"{symbol}.parquet", bar)

    final_probe = _file_max(probe)
    if final_probe != trade_date:
        raise RuntimeError(f"fallback probe validation failed: {final_probe} != {trade_date}")
    audit_dir = store.parent / "fallback_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / f"market_{trade_date}.json"
    audit_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["audit_path"] = str(audit_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", help="YYYYMMDD; default latest open day <= today")
    parser.add_argument("--store", default=os.environ.get(
        "QMT_SERVER_STORE", "/opt/qmt-server/v2.3/server/data/market/daily",
    ))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise SystemExit("TUSHARE_TOKEN is not configured")
    trade_date = args.trade_date or _latest_open_trade_day(token)
    print(json.dumps(run_fallback(
        Path(args.store), trade_date, token, dry_run=args.dry_run, force=args.force,
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
