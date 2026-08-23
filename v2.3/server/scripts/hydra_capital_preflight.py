"""按本月 target + 原始执行价估算 ETF 整手资金门槛。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from app.services.hydra_capital import (
    analyze_capital,
    minimum_capital_for_name_coverage,
)


def _read(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--execution-raw", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--capital", type=float, action="append", required=True)
    parser.add_argument("--cash-buffer", type=float, default=0.01)
    parser.add_argument("--lot-size", type=int, default=100)
    args = parser.parse_args()

    target = _read(args.target)
    raw = _read(args.execution_raw)
    if not {"code", "weight"}.issubset(target.columns):
        raise ValueError("target 必须包含 code/weight")
    if target.empty or target["code"].astype(str).duplicated().any():
        raise ValueError("target 为空或包含重复 code")
    weights = dict(zip(target["code"].astype(str), target["weight"].astype(float)))
    if not {"symbol", "trade_date", "close"}.issubset(raw.columns):
        raise ValueError("execution raw 必须包含 symbol/trade_date/close")
    dates = raw["trade_date"].astype(str).str.replace("-", "", regex=False)
    rows = raw.loc[dates == args.as_of]
    if rows["symbol"].astype(str).duplicated().any():
        raise ValueError("execution raw 的 as_of 日包含重复 symbol")
    if "suspendFlag" in rows and (rows["suspendFlag"].astype(int) != 0).any():
        raise ValueError("execution raw 的 as_of 日包含停牌标的")
    prices = dict(zip(rows["symbol"].astype(str), rows["close"].astype(float)))
    prices = {code: prices[code] for code in weights if code in prices}
    result = {
        "as_of_date": args.as_of,
        "cash_buffer": args.cash_buffer,
        "lot_size": args.lot_size,
        "minimum_capital_80pct_names": minimum_capital_for_name_coverage(
            weights, prices, 0.8,
            cash_buffer=args.cash_buffer, lot_size=args.lot_size,
        ),
        "minimum_capital_all_names": minimum_capital_for_name_coverage(
            weights, prices, 1.0,
            cash_buffer=args.cash_buffer, lot_size=args.lot_size,
        ),
        "scenarios": [
            analyze_capital(
                weights, prices, capital,
                cash_buffer=args.cash_buffer, lot_size=args.lot_size,
            ).to_dict()
            for capital in args.capital
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
