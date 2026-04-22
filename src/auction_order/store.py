"""写入 orders 表。"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    signal_id: str
    symbol: str
    direction: str
    submitted_price: float
    submitted_quantity: int
    submitted_at: str
    submit_status: str


_INSERT_SQL = """
INSERT OR REPLACE INTO orders
(order_id, signal_id, symbol, direction,
 submitted_price, submitted_quantity, submitted_at, submit_status)
VALUES (?, ?, ?, ?, ?, ?, ?, ?);
"""


def save_orders(conn: sqlite3.Connection, records: list[OrderRecord]) -> int:
    if not records:
        return 0

    rows = [
        (r.order_id, r.signal_id, r.symbol, r.direction,
         float(r.submitted_price), int(r.submitted_quantity),
         r.submitted_at, r.submit_status)
        for r in records
    ]
    conn.executemany(_INSERT_SQL, rows)
    conn.commit()
    logger.info("orders 表写入 %d 行", len(rows))
    return len(rows)
