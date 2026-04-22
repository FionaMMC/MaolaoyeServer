"""写入 signals 表。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT OR REPLACE INTO signals
(signal_id, symbol, direction, quantity, order_type, limit_price,
 price_offset, strategy_id, signal_time, valid_date, fetched_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def save_signals(
    conn: sqlite3.Connection,
    signals: list[dict[str, Any]],
) -> int:
    """按 signal_id upsert 到 signals 表。"""
    if not signals:
        return 0

    fetched_at = _now_iso()
    rows = [
        (
            s["signal_id"], s["symbol"], s["direction"],
            int(s["quantity"]), s["order_type"],
            s.get("limit_price"),
            float(s["price_offset"]), s["strategy_id"],
            s["signal_time"], s["valid_date"],
            fetched_at,
        )
        for s in signals
    ]
    conn.executemany(_UPSERT_SQL, rows)
    conn.commit()
    logger.info("signals 表 upsert %d 行，fetched_at=%s", len(rows), fetched_at)
    return len(rows)
