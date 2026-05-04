"""tests/unit/test_parquet.py — ParquetStore 单元测试"""
from pathlib import Path

import pandas as pd
import pytest

from app.storage.parquet import ParquetStore


def _row(trade_date: int, close: float = 10.0) -> dict:
    return {
        "trade_date": trade_date,
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.3,
        "close": close,
        "volume": 1000,
        "amount": close * 1000,
        "suspendFlag": 0,
    }


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_append_creates_new_file(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    n = store.append("stocks", "600519.SH", _df([_row(20260428)]))
    assert n == 1
    p = tmp_path / "market" / "daily" / "stocks" / "600519.SH.parquet"
    assert p.exists()


def test_append_dedupes_existing_dates(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH", _df([_row(20260428, close=10.0)]))
    n = store.append("stocks", "600519.SH",
                     _df([_row(20260428, close=99.0), _row(20260429)]))
    assert n == 1
    df = store.read("stocks", "600519.SH")
    assert len(df) == 2
    assert df.set_index("trade_date").loc[20260428, "close"] == 10.0


def test_append_keeps_data_sorted_by_trade_date(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH",
                 _df([_row(20260430), _row(20260428), _row(20260429)]))
    df = store.read("stocks", "600519.SH")
    assert df["trade_date"].tolist() == [20260428, 20260429, 20260430]


def test_read_full(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH",
                 _df([_row(20260428), _row(20260429), _row(20260430)]))
    df = store.read("stocks", "600519.SH")
    assert len(df) == 3
    assert set(df.columns) >= {"trade_date", "close"}


def test_read_date_range(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH",
                 _df([_row(d) for d in (20260427, 20260428, 20260429, 20260430)]))
    df = store.read("stocks", "600519.SH", start_date=20260428, end_date=20260429)
    assert df["trade_date"].tolist() == [20260428, 20260429]


def test_read_nonexistent_symbol_returns_empty(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    df = store.read("stocks", "NOT_EXIST.SH")
    assert df.empty
    assert isinstance(df, pd.DataFrame)


def test_has_date(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH", _df([_row(20260428)]))
    assert store.has_date("stocks", "600519.SH", 20260428) is True
    assert store.has_date("stocks", "600519.SH", 20260429) is False
    assert store.has_date("stocks", "NOT_EXIST.SH", 20260428) is False


def test_list_symbols_per_category(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH", _df([_row(20260428)]))
    store.append("stocks", "000001.SZ", _df([_row(20260428)]))
    store.append("indexes", "000300.SH", _df([_row(20260428)]))

    assert sorted(store.list_symbols("stocks")) == ["000001.SZ", "600519.SH"]
    assert store.list_symbols("indexes") == ["000300.SH"]
    assert store.list_symbols("etfs") == []


def test_categories_isolated(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH", _df([_row(20260428, close=1500.0)]))
    store.append("etfs", "600519.SH", _df([_row(20260428, close=2.5)]))
    s = store.read("stocks", "600519.SH")
    e = store.read("etfs", "600519.SH")
    assert s["close"].iloc[0] == 1500.0
    assert e["close"].iloc[0] == 2.5
