"""成交回报处理：拆单 + 更新虚拟账本 + 标记订单状态。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select

from app.models import InstanceState, Order, OrderSignalMap, RawSignal, Trade
from app.schemas.trade_result import TradeResult, TradeResultResponseData

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def largest_remainder_split(total: int, weights: list[int]) -> list[int]:
    """最大余数法把 total 按 weights 比例拆为整数列表（保证 sum == total）。

    若 sum(weights) == 0 或 total == 0，返回全 0。

    Examples:
        >>> largest_remainder_split(350, [100, 200, 300])
        [58, 117, 175]   # sum = 350
    """
    if total == 0 or sum(weights) == 0:
        return [0] * len(weights)
    sw = sum(weights)
    raw = [total * w / sw for w in weights]
    floors = [int(x) for x in raw]
    remainder = total - sum(floors)
    if remainder == 0:
        return floors
    fractional = [(raw[i] - floors[i], i) for i in range(len(raw))]
    fractional.sort(key=lambda t: -t[0])  # 余数从大到小
    for k in range(remainder):
        floors[fractional[k][1]] += 1
    return floors


class SettlementService:
    """成交回报处理服务。"""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def settle(
        self,
        trade_date: str,
        results: list[TradeResult],
    ) -> TradeResultResponseData:
        """处理一批成交回报。"""
        matched = 0
        unmatched: list[str] = []

        with self.session_factory() as session:
            for result in results:
                order = session.get(Order, result.order_id)
                if order is None:
                    unmatched.append(result.order_id)
                    continue

                # 写 trades 表
                session.add(Trade(
                    order_id=result.order_id,
                    filled_quantity=result.filled_quantity,
                    filled_price=result.filled_price,
                    filled_time=result.filled_time,
                    status=result.status,
                    received_at=_now_iso(),
                ))

                # 拆单 + 更新虚拟账本（仅在有成交时）
                if result.filled_quantity > 0:
                    self._split_and_update_state(
                        session, order, result.filled_quantity, result.filled_price,
                    )

                # 标记订单状态
                order.status = result.status
                matched += 1

            session.commit()

        return TradeResultResponseData(
            trade_date=trade_date,
            matched_count=matched,
            unmatched_order_ids=unmatched,
        )

    # ── 内部 ────────────────────────────────────────────────────────────
    def _split_and_update_state(
        self,
        session,
        order: Order,
        filled_qty: int,
        filled_price: float,
    ) -> None:
        # 取该 order 的所有 (signal_id, signal_quantity)
        mappings = session.execute(
            select(OrderSignalMap).where(OrderSignalMap.order_id == order.order_id)
        ).scalars().all()
        if not mappings:
            logger.warning(
                "order %s 无 order_signal_map 记录，跳过拆单更新",
                order.order_id,
            )
            return

        # 拆分
        weights = [m.signal_quantity for m in mappings]
        splits = largest_remainder_split(filled_qty, weights)

        # 对每条 signal 找 instance_id 然后更新虚拟账本
        for m, split_qty in zip(mappings, splits):
            if split_qty == 0:
                continue
            sig = session.get(RawSignal, m.signal_id)
            if sig is None:
                logger.warning(
                    "signal_id=%s 在 raw_signals 中不存在，跳过虚拟账本更新",
                    m.signal_id,
                )
                continue

            inst = session.get(InstanceState, sig.instance_id)
            if inst is None:
                logger.warning(
                    "instance_id=%s 在 instance_state 中不存在，自动创建",
                    sig.instance_id,
                )
                inst = InstanceState(
                    instance_id=sig.instance_id,
                    virtual_cash=0.0,
                    virtual_positions={},
                    last_update=_now_iso(),
                )
                session.add(inst)
                # 必须 flush 才能 get 出来
                session.flush()

            # 复制 dict（SQLAlchemy mutable JSON 需要新对象）
            positions = dict(inst.virtual_positions or {})
            sym = order.symbol
            cash_delta = filled_price * split_qty
            if order.direction == "BUY":
                # 防穿仓：现金不够就拒绝（避免重复 fill 把虚拟账本扣穿）
                if inst.virtual_cash < cash_delta:
                    logger.warning(
                        "instance %s 现金不足拒绝 BUY fill: cash=%.2f < cost=%.2f sym=%s qty=%d "
                        "（可能是 dupe orders 导致；已忽略此 fill 的账本更新）",
                        sig.instance_id, inst.virtual_cash, cash_delta, sym, split_qty,
                    )
                    continue
                inst.virtual_cash = inst.virtual_cash - cash_delta
                positions[sym] = positions.get(sym, 0) + split_qty
            else:  # SELL
                # 防超卖：持仓不够就拒绝
                cur_qty = positions.get(sym, 0)
                if cur_qty < split_qty:
                    logger.warning(
                        "instance %s 持仓不足拒绝 SELL fill: holding=%d < sell=%d sym=%s "
                        "（可能是 dupe orders；已忽略此 fill 的账本更新）",
                        sig.instance_id, cur_qty, split_qty, sym,
                    )
                    continue
                inst.virtual_cash = inst.virtual_cash + cash_delta
                new_qty = cur_qty - split_qty
                if new_qty <= 0:
                    positions.pop(sym, None)
                else:
                    positions[sym] = new_qty
            inst.virtual_positions = positions
            inst.last_update = _now_iso()
