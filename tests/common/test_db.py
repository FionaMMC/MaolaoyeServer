"""src.common.db 测试"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.common.db import get_connection, init_schema


def test_get_connection_creates_file(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        assert db.exists()
    finally:
        conn.close()


def test_init_schema_creates_three_tables(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        init_schema(conn)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {r[0] for r in cur.fetchall()}
        assert {"signals", "orders", "trades"}.issubset(tables)
    finally:
        conn.close()


def test_init_schema_is_idempotent(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        init_schema(conn)
        init_schema(conn)
        cur = conn.execute("SELECT COUNT(*) FROM signals")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_signals_table_has_expected_columns(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        init_schema(conn)
        cur = conn.execute("PRAGMA table_info(signals)")
        cols = {r[1] for r in cur.fetchall()}
        assert cols == {
            "signal_id", "symbol", "direction", "quantity", "order_type",
            "limit_price", "price_offset", "strategy_id",
            "signal_time", "valid_date", "fetched_at",
        }
    finally:
        conn.close()


def test_orders_table_has_expected_columns(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        init_schema(conn)
        cur = conn.execute("PRAGMA table_info(orders)")
        cols = {r[1] for r in cur.fetchall()}
        assert cols == {
            "order_id", "signal_id", "symbol", "direction",
            "submitted_price", "submitted_quantity", "submitted_at",
            "submit_status",
        }
    finally:
        conn.close()


def test_trades_table_has_expected_columns(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        init_schema(conn)
        cur = conn.execute("PRAGMA table_info(trades)")
        cols = {r[1] for r in cur.fetchall()}
        assert cols == {
            "id", "order_id", "signal_id",
            "filled_quantity", "filled_price", "filled_time",
            "status", "reported_at", "report_status",
        }
    finally:
        conn.close()


def test_foreign_keys_enabled(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        cur = conn.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()
