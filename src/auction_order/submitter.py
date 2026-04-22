"""调用 QMT order_stock 提交限价单。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.auction_order.price_calc import compute_submit_price
from src.auction_order.signal_reader import Signal

logger = logging.getLogger(__name__)


@dataclass
class SubmitResult:
    ok: bool
    order_id: str | None = None
    submitted_price: float | None = None
    submitted_quantity: int | None = None
    submitted_at: str | None = None
    error: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def submit_order(trader: Any, account: Any, signal: Signal) -> SubmitResult:
    """提交单条限价委托。不抛异常，失败原因放进 SubmitResult.error。"""
    if signal.order_type == "MARKET":
        return SubmitResult(
            ok=False,
            error="MARKET 单被拒（模拟盘不支持市价单）",
        )

    try:
        price = compute_submit_price(
            signal.limit_price, signal.price_offset, signal.direction,
        )
    except ValueError as e:
        return SubmitResult(ok=False, error=f"price_calc 失败: {e}")

    from xtquant import xtconstant

    direction_code = (
        xtconstant.STOCK_BUY if signal.direction == "BUY"
        else xtconstant.STOCK_SELL
    )

    try:
        order_id = trader.order_stock(
            account=account,
            stock_code=signal.symbol,
            order_type=direction_code,
            order_volume=int(signal.quantity),
            price_type=xtconstant.FIX_PRICE,
            price=price,
            strategy_name=signal.strategy_id,
            order_remark=signal.signal_id,
        )
    except Exception as e:
        return SubmitResult(ok=False, error=f"order_stock 异常: {e}")

    if not isinstance(order_id, int) or order_id < 0:
        return SubmitResult(ok=False, error=f"order_stock 返回 {order_id}")

    logger.info("signal_id=%s 委托成功 order_id=%s price=%s qty=%s",
                signal.signal_id, order_id, price, signal.quantity)
    return SubmitResult(
        ok=True,
        order_id=str(order_id),
        submitted_price=price,
        submitted_quantity=int(signal.quantity),
        submitted_at=_now_iso(),
    )
