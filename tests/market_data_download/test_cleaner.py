from __future__ import annotations

import math

import pandas as pd
import pytest

from src.market_data_download.cleaner import clean_market_data


def _make_raw(symbols: list[str], date: str, rows: dict[str, dict]) -> dict:
    fields = ["open", "high", "low", "close", "volume", "amount",
              "turnoverRatio", "suspendFlag"]
    md = {}
    for f in fields:
        df = pd.DataFrame(index=symbols, columns=[date], dtype="float64")
        for s in symbols:
            df.loc[s, date] = rows[s].get(f, 0)
        md[f] = df
    return {"trade_date": date, "symbols": symbols, "market_data": md}


def test_clean_basic_two_stocks():
    raw = _make_raw(
        symbols=["600519.SH", "000001.SZ"],
        date="20260422",
        rows={
            "600519.SH": dict(
                open=1520.0, high=1548.0, low=1515.0, close=1540.0,
                volume=12345678, amount=19012345678.0,
                turnoverRatio=0.0032, suspendFlag=0,
            ),
            "000001.SZ": dict(
                open=10.0, high=10.5, low=9.8, close=10.2,
                volume=100_000, amount=1_020_000.0,
                turnoverRatio=0.01, suspendFlag=0,
            ),
        },
    )

    df = clean_market_data(raw)

    assert list(df.columns) == [
        "symbol", "trade_date", "open", "high", "low", "close",
        "volume", "amount", "turnover_rate", "is_suspended",
    ]
    assert len(df) == 2
    row = df.set_index("symbol").loc["600519.SH"]
    assert row["trade_date"] == "20260422"
    assert row["close"] == 1540.0
    assert not bool(row["is_suspended"])
    assert math.isclose(float(row["turnover_rate"]), 0.0032)
    assert df["volume"].dtype.kind in ("i", "u")


def test_clean_suspended_preserves_row():
    raw = _make_raw(
        symbols=["600000.SH"],
        date="20260422",
        rows={
            "600000.SH": dict(
                open=0, high=0, low=0, close=0,
                volume=0, amount=0,
                turnoverRatio=0, suspendFlag=1,
            ),
        },
    )

    df = clean_market_data(raw)

    assert len(df) == 1
    assert bool(df.iloc[0]["is_suspended"])
    assert df.iloc[0]["symbol"] == "600000.SH"


def test_clean_drops_zero_ohlcv_non_suspended():
    """非停牌但 OHLCV 全 0 的行应被丢弃"""
    raw = _make_raw(
        symbols=["GOOD.SH", "GARBAGE.SH"],
        date="20260422",
        rows={
            "GOOD.SH": dict(
                open=10, high=10.5, low=9.8, close=10.2,
                volume=1000, amount=10000.0,
                turnoverRatio=0.01, suspendFlag=0,
            ),
            "GARBAGE.SH": dict(
                open=0, high=0, low=0, close=0,
                volume=0, amount=0,
                turnoverRatio=0, suspendFlag=0,
            ),
        },
    )

    df = clean_market_data(raw)

    assert set(df["symbol"]) == {"GOOD.SH"}


def test_clean_empty_symbols_raises():
    raw = {"trade_date": "20260422", "symbols": [], "market_data": {}}
    with pytest.raises(ValueError, match="空"):
        clean_market_data(raw)
