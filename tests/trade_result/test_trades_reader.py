"""trades_reader 测试"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.trade_result.trades_reader import QmtTrade, fetch_trades


def _xt_trade(oid: int, code: str, price: float, vol: int, ts: int):
    return SimpleNamespace(
        order_id=oid,
        stock_code=code,
        traded_price=price,
        traded_volume=vol,
        traded_amount=price * vol,
        traded_time=ts,
    )


def test_fetch_trades_happy_path():
    trader = MagicMock()
    trader.query_stock_trades = MagicMock(return_value=[
        _xt_trade(111, "600519.SH", 1547.7, 100, 1745284500),
        _xt_trade(111, "600519.SH", 1547.8, 50, 1745284600),
        _xt_trade(222, "000001.SZ", 10.0, 300, 1745284700),
    ])
    account = SimpleNamespace(account_id="ACC")

    trades = fetch_trades(trader, account)

    assert len(trades) == 3
    assert all(isinstance(t, QmtTrade) for t in trades)
    assert trades[0].order_id == "111"
    assert trades[0].stock_code == "600519.SH"
    assert trades[0].traded_price == 1547.7
    assert trades[0].traded_volume == 100
    assert "T" in trades[0].traded_time


def test_fetch_trades_empty():
    trader = MagicMock()
    trader.query_stock_trades = MagicMock(return_value=[])
    account = SimpleNamespace(account_id="ACC")

    trades = fetch_trades(trader, account)
    assert trades == []


def test_fetch_trades_none_returned_treated_as_empty():
    trader = MagicMock()
    trader.query_stock_trades = MagicMock(return_value=None)
    account = SimpleNamespace(account_id="ACC")

    trades = fetch_trades(trader, account)
    assert trades == []


def test_fetch_trades_query_exception_raises():
    import pytest
    trader = MagicMock()
    trader.query_stock_trades = MagicMock(side_effect=RuntimeError("boom"))
    account = SimpleNamespace(account_id="ACC")

    with pytest.raises(RuntimeError, match="boom"):
        fetch_trades(trader, account)
