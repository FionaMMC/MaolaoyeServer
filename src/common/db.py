"""SQLite 连接封装 + 三表 schema 初始化。"""
from __future__ import annotations

import sqlite3
from pathlib import Path


_SIGNALS_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id     TEXT PRIMARY KEY,
    symbol        TEXT NOT NULL,
    direction     TEXT NOT NULL,
    quantity      INTEGER NOT NULL,
    order_type    TEXT NOT NULL,
    limit_price   REAL,
    price_offset  REAL NOT NULL,
    strategy_id   TEXT NOT NULL,
    signal_time   TEXT NOT NULL,
    valid_date    TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);
"""

_ORDERS_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    order_id            TEXT PRIMARY KEY,
    signal_id           TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    submitted_price     REAL NOT NULL,
    submitted_quantity  INTEGER NOT NULL,
    submitted_at        TEXT NOT NULL,
    submit_status       TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
);
"""

_TRADES_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id         TEXT NOT NULL,
    signal_id        TEXT NOT NULL,
    filled_quantity  INTEGER NOT NULL,
    filled_price     REAL NOT NULL,
    filled_time      TEXT,
    status           TEXT NOT NULL,
    reported_at      TEXT,
    report_status    TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
"""


def get_connection(path: Path | str) -> sqlite3.Connection:
    """返回启用外键的 SQLite 连接，文件不存在会自动创建。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等创建三张表。"""
    conn.execute(_SIGNALS_SQL)
    conn.execute(_ORDERS_SQL)
    conn.execute(_TRADES_SQL)
    conn.commit()
