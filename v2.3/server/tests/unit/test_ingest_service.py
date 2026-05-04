"""tests/unit/test_ingest_service.py"""
from pathlib import Path

import pandas as pd
import pytest

from app.exceptions import APIError, ErrorCode
from app.schemas.market_data import (
    ETFBar, IndexBar, MarketDataRequest, StockBar,
)
from app.services.ingest import IngestService
from app.storage.parquet import ParquetStore


def _stock(symbol="600519.SH", suspended=False, **overrides):
    base = dict(symbol=symbol, open=10.0, high=11.0, low=9.0, close=10.5,
                volume=1000, amount=10500.0, is_suspended=suspended)
    base.update(overrides)
    return StockBar(**base)


def _index(symbol="000300.SH", **overrides):
    base = dict(symbol=symbol, open=3800.0, high=3850.0, low=3790.0,
                close=3820.0, volume=0, amount=0.0)
    base.update(overrides)
    return IndexBar(**base)


def _etf(symbol="510300.SH", suspended=False, **overrides):
    base = dict(symbol=symbol, open=3.8, high=3.85, low=3.79, close=3.82,
                volume=100000, amount=380000.0, is_suspended=suspended)
    base.update(overrides)
    return ETFBar(**base)


def _svc(tmp_path: Path) -> IngestService:
    return IngestService(parquet_store=ParquetStore(root=tmp_path))


def test_ingest_writes_stocks_to_parquet(tmp_path: Path):
    svc = _svc(tmp_path)
    req = MarketDataRequest(trade_date="20260430", stocks=[_stock()],
                            indexes=[], etfs=[])
    resp = svc.ingest(req)
    assert resp.received.stocks == 1

    df = pd.read_parquet(tmp_path / "market" / "daily" / "stocks" / "600519.SH.parquet")
    assert list(df["trade_date"]) == [20260430]
    assert df["close"].iloc[0] == 10.5


def test_ingest_translates_is_suspended_to_int(tmp_path: Path):
    svc = _svc(tmp_path)
    req = MarketDataRequest(
        trade_date="20260430",
        stocks=[_stock(symbol="A.SH", suspended=True),
                _stock(symbol="B.SH", suspended=False)],
        indexes=[], etfs=[],
    )
    svc.ingest(req)

    df_a = pd.read_parquet(tmp_path / "market" / "daily" / "stocks" / "A.SH.parquet")
    df_b = pd.read_parquet(tmp_path / "market" / "daily" / "stocks" / "B.SH.parquet")
    assert df_a["suspendFlag"].iloc[0] == 1
    assert df_b["suspendFlag"].iloc[0] == 0
    assert "is_suspended" not in df_a.columns


def test_ingest_indexes_no_suspendFlag(tmp_path: Path):
    svc = _svc(tmp_path)
    req = MarketDataRequest(trade_date="20260430", stocks=[],
                            indexes=[_index()], etfs=[])
    svc.ingest(req)

    df = pd.read_parquet(tmp_path / "market" / "daily" / "indexes" / "000300.SH.parquet")
    assert "suspendFlag" not in df.columns
    assert df["close"].iloc[0] == 3820.0


def test_ingest_etfs_with_suspendFlag(tmp_path: Path):
    svc = _svc(tmp_path)
    req = MarketDataRequest(trade_date="20260430", stocks=[],
                            indexes=[], etfs=[_etf(suspended=True)])
    svc.ingest(req)

    df = pd.read_parquet(tmp_path / "market" / "daily" / "etfs" / "510300.SH.parquet")
    assert df["suspendFlag"].iloc[0] == 1


def test_ingest_drops_turnover_rate(tmp_path: Path):
    """v2.3 设计：不存 turnover_rate（QMT 日线不可靠）。"""
    svc = _svc(tmp_path)
    s = _stock()
    s.turnover_rate = 0.05
    req = MarketDataRequest(trade_date="20260430", stocks=[s], indexes=[], etfs=[])
    svc.ingest(req)

    df = pd.read_parquet(tmp_path / "market" / "daily" / "stocks" / "600519.SH.parquet")
    assert "turnover_rate" not in df.columns


def test_ingest_empty_arrays_raises_2002(tmp_path: Path):
    svc = _svc(tmp_path)
    req = MarketDataRequest(trade_date="20260430", stocks=[], indexes=[], etfs=[])
    with pytest.raises(APIError) as ei:
        svc.ingest(req)
    assert ei.value.code == ErrorCode.EMPTY_DATA


def test_ingest_full_duplicate_raises_2001(tmp_path: Path):
    svc = _svc(tmp_path)
    req1 = MarketDataRequest(trade_date="20260430", stocks=[_stock()],
                             indexes=[], etfs=[])
    svc.ingest(req1)
    with pytest.raises(APIError) as ei:
        svc.ingest(req1)
    assert ei.value.code == ErrorCode.DUPLICATE_DATE


def test_ingest_partial_duplicate_returns_correct_count(tmp_path: Path):
    svc = _svc(tmp_path)
    svc.ingest(MarketDataRequest(
        trade_date="20260430", stocks=[_stock(symbol="A.SH")],
        indexes=[], etfs=[],
    ))
    resp = svc.ingest(MarketDataRequest(
        trade_date="20260430",
        stocks=[_stock(symbol="A.SH"), _stock(symbol="B.SH")],
        indexes=[], etfs=[],
    ))
    assert resp.received.stocks == 1   # 只 B 是新的
    assert resp.strategy_triggered is True


def test_ingest_strategy_triggered_true_on_success(tmp_path: Path):
    svc = _svc(tmp_path)
    resp = svc.ingest(MarketDataRequest(
        trade_date="20260430", stocks=[_stock()], indexes=[], etfs=[],
    ))
    assert resp.strategy_triggered is True
