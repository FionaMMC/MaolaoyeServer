"""signal_reader 测试"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.db import get_connection, init_schema
from src.auction_order.signal_reader import Signal, read_active_signals


def _insert_signal(conn, sid, valid_date, **overrides):
    d = dict(
        signal_id=sid, symbol="600519.SH", direction="BUY", quantity=100,
        order_type="LIMIT", limit_price=1540.0, price_offset=0.005,
        strategy_id="s", signal_time="2026-04-21T18:30:00+08:00",
        valid_date=valid_date, fetched_at="2026-04-21T19:00:00+08:00",
    )
    d.update(overrides)
    conn.execute(
        """INSERT INTO signals
        (signal_id, symbol, direction, quantity, order_type, limit_price,
         price_offset, strategy_id, signal_time, valid_date, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (d["signal_id"], d["symbol"], d["direction"], d["quantity"],
         d["order_type"], d["limit_price"], d["price_offset"],
         d["strategy_id"], d["signal_time"], d["valid_date"], d["fetched_at"]),
    )
    conn.commit()


def _insert_order(conn, order_id, signal_id):
    conn.execute(
        """INSERT INTO orders
        (order_id, signal_id, symbol, direction, submitted_price,
         submitted_quantity, submitted_at, submit_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_id, signal_id, "600519.SH", "BUY",
         1547.7, 100, "2026-04-22T09:15:00+08:00", "SUCCESS"),
    )
    conn.commit()


def test_read_returns_signals_with_matching_valid_date(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _insert_signal(conn, "a", "20260422")
    _insert_signal(conn, "b", "20260422")
    _insert_signal(conn, "c", "20260423")

    sigs = read_active_signals(conn, today="20260422")

    assert [s.signal_id for s in sigs] == ["a", "b"]
    assert isinstance(sigs[0], Signal)
    assert sigs[0].symbol == "600519.SH"
    assert sigs[0].quantity == 100


def test_read_skips_signals_already_in_orders(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _insert_signal(conn, "a", "20260422")
    _insert_signal(conn, "b", "20260422")
    _insert_order(conn, order_id="order-a", signal_id="a")

    sigs = read_active_signals(conn, today="20260422")

    assert [s.signal_id for s in sigs] == ["b"]


def test_read_empty_when_no_signals(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)

    sigs = read_active_signals(conn, today="20260422")
    assert sigs == []


def test_read_preserves_all_fields(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _insert_signal(conn, "a", "20260422",
                   direction="SELL", price_offset=-0.003,
                   limit_price=99.5, quantity=300)

    sigs = read_active_signals(conn, today="20260422")

    s = sigs[0]
    assert s.signal_id == "a"
    assert s.direction == "SELL"
    assert s.price_offset == -0.003
    assert s.limit_price == 99.5
    assert s.quantity == 300
    assert s.order_type == "LIMIT"


def test_read_null_limit_price_is_preserved(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _insert_signal(conn, "a", "20260422",
                   order_type="MARKET", limit_price=None)

    sigs = read_active_signals(conn, today="20260422")
    assert sigs[0].order_type == "MARKET"
    assert sigs[0].limit_price is None
