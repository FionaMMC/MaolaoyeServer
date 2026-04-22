"""matcher 测试"""
from __future__ import annotations

from src.trade_result.matcher import TradeRecord, match_trades
from src.trade_result.trades_reader import QmtTrade


def _order(oid="o1", sid="s1", qty=100, direction="BUY"):
    return {
        "order_id": oid, "signal_id": sid,
        "symbol": "600519.SH", "direction": direction,
        "submitted_quantity": qty,
    }


def _qt(oid, price, vol, ts="2026-04-22T09:25:00+08:00"):
    return QmtTrade(
        order_id=oid, stock_code="600519.SH",
        traded_price=price, traded_volume=vol,
        traded_amount=price * vol, traded_time=ts,
    )


def test_match_filled_single_trade():
    orders = [_order("o1", "s1", qty=100)]
    trades = [_qt("o1", 10.0, 100)]

    records = match_trades(trades, orders)

    assert len(records) == 1
    r = records[0]
    assert isinstance(r, TradeRecord)
    assert r.order_id == "o1"
    assert r.signal_id == "s1"
    assert r.filled_quantity == 100
    assert r.filled_price == 10.0
    assert r.status == "FILLED"


def test_match_partial_single_trade():
    orders = [_order("o1", "s1", qty=100)]
    trades = [_qt("o1", 10.0, 30)]

    records = match_trades(trades, orders)

    assert records[0].filled_quantity == 30
    assert records[0].status == "PARTIAL"


def test_match_multiple_trades_aggregated():
    orders = [_order("o1", "s1", qty=100)]
    trades = [
        _qt("o1", 10.0, 30, ts="2026-04-22T09:25:00+08:00"),
        _qt("o1", 10.2, 70, ts="2026-04-22T09:26:00+08:00"),
    ]

    records = match_trades(trades, orders)

    r = records[0]
    assert r.filled_quantity == 100
    assert abs(r.filled_price - 10.14) < 1e-6
    assert r.filled_time == "2026-04-22T09:26:00+08:00"
    assert r.status == "FILLED"


def test_match_order_without_trades_is_cancelled():
    orders = [_order("o1", "s1", qty=100)]
    trades: list[QmtTrade] = []

    records = match_trades(trades, orders)

    assert len(records) == 1
    r = records[0]
    assert r.filled_quantity == 0
    assert r.filled_price == 0.0
    assert r.filled_time is None
    assert r.status == "CANCELLED"


def test_match_trade_without_order_is_ignored():
    orders = [_order("o1", "s1", qty=100)]
    trades = [
        _qt("o1", 10.0, 100),
        _qt("o99", 50.0, 50),
    ]

    records = match_trades(trades, orders)

    assert [r.order_id for r in records] == ["o1"]


def test_match_mixed_orders_some_filled_some_not():
    orders = [
        _order("o1", "s1", qty=100),
        _order("o2", "s2", qty=200),
        _order("o3", "s3", qty=300),
    ]
    trades = [
        _qt("o1", 10.0, 100),
        _qt("o2", 20.0, 50),
    ]

    records = match_trades(trades, orders)

    by_sid = {r.signal_id: r for r in records}
    assert by_sid["s1"].status == "FILLED"
    assert by_sid["s2"].status == "PARTIAL"
    assert by_sid["s3"].status == "CANCELLED"
    assert by_sid["s3"].filled_quantity == 0
