"""store 测试"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.db import get_connection, init_schema
from src.signal_query.store import save_signals


def _sig(sid: str = "a", valid: str = "20260422") -> dict:
    return {
        "signal_id": sid,
        "symbol": "600519.SH",
        "direction": "BUY",
        "quantity": 100,
        "order_type": "LIMIT",
        "limit_price": 1540.0,
        "price_offset": 0.005,
        "strategy_id": "s1",
        "signal_time": "2026-04-21T18:30:00+08:00",
        "valid_date": valid,
    }


def test_save_signals_inserts_rows(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = get_connection(db)
    init_schema(conn)

    n = save_signals(conn, [_sig("a"), _sig("b")])
    assert n == 2

    cur = conn.execute("SELECT signal_id, symbol, direction FROM signals ORDER BY signal_id")
    rows = cur.fetchall()
    assert [r[0] for r in rows] == ["a", "b"]
    assert rows[0][1] == "600519.SH"
    conn.close()


def test_save_signals_fills_fetched_at(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = get_connection(db)
    init_schema(conn)

    save_signals(conn, [_sig("a")])

    cur = conn.execute("SELECT fetched_at FROM signals WHERE signal_id='a'")
    fetched = cur.fetchone()[0]
    assert fetched
    assert "T" in fetched
    conn.close()


def test_save_signals_upsert_overwrites_existing(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = get_connection(db)
    init_schema(conn)

    save_signals(conn, [_sig("a")])
    s2 = _sig("a")
    s2["quantity"] = 999
    save_signals(conn, [s2])

    cur = conn.execute("SELECT quantity FROM signals WHERE signal_id='a'")
    assert cur.fetchone()[0] == 999
    cur2 = conn.execute("SELECT COUNT(*) FROM signals")
    assert cur2.fetchone()[0] == 1
    conn.close()


def test_save_signals_empty_list_returns_zero(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = get_connection(db)
    init_schema(conn)

    n = save_signals(conn, [])
    assert n == 0
    conn.close()


def test_save_signals_null_limit_price_for_market(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = get_connection(db)
    init_schema(conn)

    sig = _sig("a")
    sig["order_type"] = "MARKET"
    sig["limit_price"] = None
    save_signals(conn, [sig])

    cur = conn.execute("SELECT limit_price FROM signals WHERE signal_id='a'")
    assert cur.fetchone()[0] is None
    conn.close()
