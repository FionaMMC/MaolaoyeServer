"""写入 trades 表：DELETE+INSERT 保证同 order_id 同 stage 可覆盖。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from src.trade_result.matcher import TradeRecord

logger = logging.getLogger(__name__)

_INSERT_SQL = """
INSERT INTO trades
(order_id, signal_id, filled_quantity, filled_price, filled_time,
 status, reported_at, report_status)
VALUES (?, ?, ?, ?, ?, ?, NULL, NULL);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def save_trades(
    conn: sqlite3.Connection,
    records: list[TradeRecord],
    stage: str,
) -> int:
    """幂等写入：先删本次涉及的 order_id，再插。"""
    if not records:
        return 0

    oids = [r.order_id for r in records]
    placeholders = ",".join("?" * len(oids))
    conn.execute(f"DELETE FROM trades WHERE order_id IN ({placeholders})", oids)

    rows = [
        (r.order_id, r.signal_id, r.filled_quantity, r.filled_price,
         r.filled_time, r.status)
        for r in records
    ]
    conn.executemany(_INSERT_SQL, rows)
    conn.commit()
    logger.info("trades 表 stage=%s 写入 %d 行", stage, len(rows))
    return len(rows)


def mark_reported(
    conn: sqlite3.Connection,
    order_ids: list[str],
    report_status: str,
) -> None:
    """推送完成后回标 reported_at + report_status。"""
    if not order_ids:
        return
    placeholders = ",".join("?" * len(order_ids))
    ts = _now_iso()
    conn.execute(
        f"UPDATE trades SET reported_at = ?, report_status = ? "
        f"WHERE order_id IN ({placeholders})",
        (ts, report_status, *order_ids),
    )
    conn.commit()
