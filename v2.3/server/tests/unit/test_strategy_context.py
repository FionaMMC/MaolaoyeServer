"""Context 单元测试"""
from pathlib import Path

import pandas as pd

from app.storage.parquet import ParquetStore
from app.strategy.context import Context


def _row(d: int, close: float = 10.0) -> dict:
    return {"trade_date": d, "open": close, "high": close, "low": close,
            "close": close, "volume": 1000, "amount": close * 1000,
            "suspendFlag": 0}


def _ctx(tmp_path: Path, **overrides) -> Context:
    store = ParquetStore(root=tmp_path)
    defaults = dict(
        instance_id="real_A_momentum", trade_date=20260430,
        virtual_cash=1_000_000.0, virtual_positions={},
        parquet_store=store,
    )
    defaults.update(overrides)
    return Context(**defaults)


def test_context_cash(tmp_path: Path):
    ctx = _ctx(tmp_path, virtual_cash=500_000.0)
    assert ctx.cash() == 500_000.0


def test_context_position_existing(tmp_path: Path):
    ctx = _ctx(tmp_path, virtual_positions={"600519.SH": 200, "000001.SZ": 500})
    assert ctx.position("600519.SH") == 200
    assert ctx.position("000001.SZ") == 500


def test_context_position_nonexistent_returns_zero(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assert ctx.position("NOT_HELD.SH") == 0


def test_context_positions_returns_copy(tmp_path: Path):
    ctx = _ctx(tmp_path, virtual_positions={"A.SH": 100})
    snapshot = ctx.positions()
    snapshot["A.SH"] = 999
    assert ctx.position("A.SH") == 100


def test_context_market_returns_history(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH",
                 pd.DataFrame([_row(20260427), _row(20260428), _row(20260430)]))
    ctx = Context(instance_id="x", trade_date=20260430,
                  virtual_cash=0.0, virtual_positions={},
                  parquet_store=store)
    df = ctx.market("600519.SH")
    assert df["trade_date"].tolist() == [20260427, 20260428, 20260430]


def test_context_market_default_end_is_trade_date(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "A.SH",
                 pd.DataFrame([_row(20260428), _row(20260429), _row(20260430)]))
    ctx = Context(instance_id="x", trade_date=20260429,
                  virtual_cash=0.0, virtual_positions={},
                  parquet_store=store)
    df = ctx.market("A.SH")
    assert df["trade_date"].max() == 20260429


def test_context_market_with_fields_filter(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "A.SH", pd.DataFrame([_row(20260430)]))
    ctx = Context(instance_id="x", trade_date=20260430,
                  virtual_cash=0.0, virtual_positions={},
                  parquet_store=store)
    df = ctx.market("A.SH", fields=["close"])
    assert set(df.columns) == {"trade_date", "close"}


def test_context_market_nonexistent_symbol_empty(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assert ctx.market("NOT_EXIST.SH").empty


def test_context_universe(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "A.SH", pd.DataFrame([_row(20260430)]))
    store.append("stocks", "B.SH", pd.DataFrame([_row(20260430)]))
    store.append("indexes", "000300.SH", pd.DataFrame([_row(20260430)]))
    ctx = Context(instance_id="x", trade_date=20260430,
                  virtual_cash=0.0, virtual_positions={},
                  parquet_store=store)
    assert sorted(ctx.universe("stocks")) == ["A.SH", "B.SH"]
    assert ctx.universe("indexes") == ["000300.SH"]
    assert ctx.universe("etfs") == []
