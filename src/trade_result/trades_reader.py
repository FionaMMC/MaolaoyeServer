"""封装 trader.query_stock_trades 返回。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QmtTrade:
    order_id: str
    stock_code: str
    traded_price: float
    traded_volume: int
    traded_amount: float
    traded_time: str


def _ts_to_iso(ts: int) -> str:
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone()
    return dt.isoformat(timespec="seconds")


def fetch_trades(trader: Any, account: Any) -> list[QmtTrade]:
    raw = trader.query_stock_trades(account) or []
    out: list[QmtTrade] = []
    for r in raw:
        out.append(QmtTrade(
            order_id=str(int(r.order_id)),
            stock_code=str(r.stock_code),
            traded_price=float(r.traded_price),
            traded_volume=int(r.traded_volume),
            traded_amount=float(r.traded_amount),
            traded_time=_ts_to_iso(r.traded_time),
        ))
    logger.info("query_stock_trades 返回 %d 条", len(out))
    return out
