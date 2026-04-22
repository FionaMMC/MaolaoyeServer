"""market_data_push.payload 测试"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.market_data_push.payload import build_market_data_payload


def _write_parquet(path: Path, rows: list[dict]) -> Path:
    df = pd.DataFrame(rows)
    df["volume"] = df["volume"].astype("int64")
    df["is_suspended"] = df["is_suspended"].astype("bool")
    out = path / "20260422.parquet"
    df.to_parquet(out, index=False)
    return out


def test_build_payload_basic(tmp_path: Path):
    pq = _write_parquet(tmp_path, [
        dict(symbol="600519.SH", trade_date="20260422",
             open=1520.0, high=1548.0, low=1515.0, close=1540.0,
             volume=12345678, amount=19012345678.0,
             turnover_rate=0.0032, is_suspended=False),
        dict(symbol="000001.SZ", trade_date="20260422",
             open=10.0, high=10.5, low=9.8, close=10.2,
             volume=100000, amount=1020000.0,
             turnover_rate=0.01, is_suspended=False),
    ])

    payload = build_market_data_payload(pq, trade_date="20260422")

    assert payload["trade_date"] == "20260422"
    assert isinstance(payload["stocks"], list)
    assert len(payload["stocks"]) == 2

    s0 = payload["stocks"][0]
    assert set(s0.keys()) == {
        "symbol", "open", "high", "low", "close",
        "volume", "amount", "turnover_rate", "is_suspended",
    }
    assert isinstance(s0["volume"], int)
    assert isinstance(s0["amount"], float)
    assert isinstance(s0["is_suspended"], bool)
    assert s0["symbol"] == "600519.SH"


def test_build_payload_rejects_mismatched_trade_date(tmp_path: Path):
    pq = _write_parquet(tmp_path, [
        dict(symbol="600519.SH", trade_date="20260422",
             open=1.0, high=1.0, low=1.0, close=1.0,
             volume=100, amount=100.0,
             turnover_rate=0.001, is_suspended=False),
    ])

    with pytest.raises(ValueError, match="trade_date"):
        build_market_data_payload(pq, trade_date="20260421")


def test_build_payload_empty_rows_raises(tmp_path: Path):
    df = pd.DataFrame(columns=[
        "symbol", "trade_date", "open", "high", "low", "close",
        "volume", "amount", "turnover_rate", "is_suspended",
    ])
    out = tmp_path / "20260422.parquet"
    df.to_parquet(out, index=False)

    with pytest.raises(ValueError, match="空"):
        build_market_data_payload(out, trade_date="20260422")
