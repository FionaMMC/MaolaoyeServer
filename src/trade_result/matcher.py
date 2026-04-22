"""把 QMT 成交与本地 orders 按 order_id 聚合匹配。"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping

from src.trade_result.trades_reader import QmtTrade

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeRecord:
    order_id: str
    signal_id: str
    symbol: str
    direction: str
    submitted_quantity: int
    filled_quantity: int
    filled_price: float
    filled_time: str | None
    status: str


def match_trades(
    trades: list[QmtTrade],
    orders: list[Mapping[str, object]],
) -> list[TradeRecord]:
    by_order: dict[str, list[QmtTrade]] = defaultdict(list)
    for t in trades:
        by_order[t.order_id].append(t)

    result: list[TradeRecord] = []
    for o in orders:
        oid = str(o["order_id"])
        its = by_order.get(oid, [])
        total_vol = sum(t.traded_volume for t in its)
        total_amt = sum(t.traded_amount for t in its)
        avg_price = (total_amt / total_vol) if total_vol > 0 else 0.0
        last_time = (max(t.traded_time for t in its) if its else None)
        submitted_qty = int(o["submitted_quantity"])

        if total_vol == 0:
            status = "CANCELLED"
        elif total_vol >= submitted_qty:
            status = "FILLED"
        else:
            status = "PARTIAL"

        result.append(TradeRecord(
            order_id=oid,
            signal_id=str(o["signal_id"]),
            symbol=str(o["symbol"]),
            direction=str(o["direction"]),
            submitted_quantity=submitted_qty,
            filled_quantity=total_vol,
            filled_price=round(avg_price, 4),
            filled_time=last_time,
            status=status,
        ))

    ignored = set(by_order.keys()) - {str(o["order_id"]) for o in orders}
    if ignored:
        logger.warning("QMT 成交记录中 %d 个 order_id 未在本地 orders 表中：%s",
                       len(ignored), sorted(ignored)[:5])
    return result
