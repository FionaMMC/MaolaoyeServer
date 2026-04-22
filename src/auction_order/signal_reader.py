"""从 SQLite signals 表读取当日有效信号（排除已下单的）。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    signal_id: str
    symbol: str
    direction: str
    quantity: int
    order_type: str
    limit_price: float | None
    price_offset: float
    strategy_id: str
    signal_time: str
    valid_date: str


_SQL = """
SELECT s.signal_id, s.symbol, s.direction, s.quantity, s.order_type,
       s.limit_price, s.price_offset, s.strategy_id,
       s.signal_time, s.valid_date
FROM signals s
WHERE s.valid_date = ?
  AND NOT EXISTS (
    SELECT 1 FROM orders o WHERE o.signal_id = s.signal_id
  )
ORDER BY s.signal_id
"""


def read_active_signals(conn: sqlite3.Connection, today: str) -> list[Signal]:
    """返回 valid_date=today 且尚未下过单的信号。"""
    cur = conn.execute(_SQL, (today,))
    return [
        Signal(
            signal_id=r["signal_id"],
            symbol=r["symbol"],
            direction=r["direction"],
            quantity=int(r["quantity"]),
            order_type=r["order_type"],
            limit_price=(None if r["limit_price"] is None else float(r["limit_price"])),
            price_offset=float(r["price_offset"]),
            strategy_id=r["strategy_id"],
            signal_time=r["signal_time"],
            valid_date=r["valid_date"],
        )
        for r in cur.fetchall()
    ]
