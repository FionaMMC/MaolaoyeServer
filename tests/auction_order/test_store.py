"""auction_order.store 测试"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.db import get_connection, init_schema
from src.auction_order.store import OrderRecord, save_orders


def _signal_row(conn, sid: str = "sig1"):
    conn.execute(
        """INSERT INTO signals
        (signal_id, symbol, direction, quantity, order_type, limit_price,
         price_offset, strategy_id, signal_time, valid_date, fetched_at)
        VALUES (?, '600519.SH', 'BUY', 100, 'LIMIT', 1540.0, 0.005,
                's', '2026-04-21T18:30:00+08:00', '20260422',
                '2026-04-21T19:00:00+08:00')""",
        (sid,),
    )
    conn.commit()


def test_save_orders_success(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _signal_row(conn, "sig1")

    recs = [OrderRecord(
        order_id="123456", signal_id="sig1", symbol="600519.SH",
        direction="BUY", submitted_price=1547.7, submitted_quantity=100,
        submitted_at="2026-04-22T09:15:00+08:00", submit_status="SUCCESS",
    )]

    n = save_orders(conn, recs)
    assert n == 1

    row = conn.execute(
        "SELECT order_id, signal_id, submit_status FROM orders"
    ).fetchone()
    assert row[0] == "123456"
    assert row[1] == "sig1"
    assert row[2] == "SUCCESS"


def test_save_orders_failed_uses_placeholder_id(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _signal_row(conn, "sig1")

    recs = [OrderRecord(
        order_id="fail-sig1", signal_id="sig1", symbol="600519.SH",
        direction="BUY", submitted_price=0.0, submitted_quantity=0,
        submitted_at="2026-04-22T09:15:00+08:00", submit_status="FAILED",
    )]

    save_orders(conn, recs)

    row = conn.execute(
        "SELECT order_id, submit_status FROM orders WHERE signal_id='sig1'"
    ).fetchone()
    assert row[0] == "fail-sig1"
    assert row[1] == "FAILED"


def test_save_orders_empty_list(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)

    assert save_orders(conn, []) == 0


def test_save_orders_multiple(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _signal_row(conn, "a")
    _signal_row(conn, "b")

    recs = [
        OrderRecord("o1", "a", "600519.SH", "BUY", 100.0, 100,
                    "2026-04-22T09:15:00+08:00", "SUCCESS"),
        OrderRecord("fail-b", "b", "000001.SZ", "BUY", 0, 0,
                    "2026-04-22T09:15:01+08:00", "FAILED"),
    ]

    n = save_orders(conn, recs)
    assert n == 2
    cnt = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert cnt == 2
