"""trade_result.store 测试"""
from __future__ import annotations

from pathlib import Path

from src.common.db import get_connection, init_schema
from src.trade_result.matcher import TradeRecord
from src.trade_result.store import mark_reported, save_trades


def _seed_order(conn, order_id, signal_id):
    conn.execute(
        """INSERT INTO signals
        (signal_id, symbol, direction, quantity, order_type, limit_price,
         price_offset, strategy_id, signal_time, valid_date, fetched_at)
        VALUES (?, '600519.SH', 'BUY', 100, 'LIMIT', 10.0, 0.005, 's',
                '2026-04-21T18:30:00+08:00', '20260422', '2026-04-21T19:00:00+08:00')""",
        (signal_id,),
    )
    conn.execute(
        """INSERT INTO orders
        (order_id, signal_id, symbol, direction, submitted_price,
         submitted_quantity, submitted_at, submit_status)
        VALUES (?, ?, '600519.SH', 'BUY', 10.0, 100,
                '2026-04-22T09:15:00+08:00', 'SUCCESS')""",
        (order_id, signal_id),
    )
    conn.commit()


def _rec(oid="o1", sid="s1", qty=100, price=10.0,
         ft="2026-04-22T09:25:00+08:00", status="FILLED") -> TradeRecord:
    return TradeRecord(
        order_id=oid, signal_id=sid, symbol="600519.SH", direction="BUY",
        submitted_quantity=100, filled_quantity=qty, filled_price=price,
        filled_time=ft, status=status,
    )


def test_save_trades_inserts(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _seed_order(conn, "o1", "s1")

    n = save_trades(conn, [_rec()], stage="auction")

    assert n == 1
    row = conn.execute(
        "SELECT order_id, signal_id, filled_quantity, status, reported_at "
        "FROM trades WHERE order_id='o1'"
    ).fetchone()
    assert row[0] == "o1"
    assert row[1] == "s1"
    assert row[2] == 100
    assert row[3] == "FILLED"
    assert row[4] is None


def test_save_trades_replaces_existing_for_same_order(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _seed_order(conn, "o1", "s1")

    save_trades(conn, [_rec(qty=30, status="PARTIAL")], stage="auction")
    save_trades(conn, [_rec(qty=100, status="FILLED")], stage="close")

    rows = conn.execute(
        "SELECT filled_quantity, status FROM trades WHERE order_id='o1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 100
    assert rows[0][1] == "FILLED"


def test_save_trades_null_filled_time(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _seed_order(conn, "o1", "s1")

    rec = _rec(qty=0, price=0.0, ft=None, status="CANCELLED")
    save_trades(conn, [rec], stage="auction")

    row = conn.execute(
        "SELECT filled_time, status FROM trades WHERE order_id='o1'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == "CANCELLED"


def test_save_trades_empty_list(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    assert save_trades(conn, [], stage="auction") == 0


def test_mark_reported_updates_status(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _seed_order(conn, "o1", "s1")
    save_trades(conn, [_rec()], stage="auction")

    mark_reported(conn, order_ids=["o1"], report_status="SUCCESS")

    row = conn.execute(
        "SELECT reported_at, report_status FROM trades WHERE order_id='o1'"
    ).fetchone()
    assert row[0] is not None
    assert row[1] == "SUCCESS"
