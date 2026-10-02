"""信号归集引擎。

合并规则：
- 分组键: (account_group, symbol, direction)
- quantity: 求和
- limit_price: BUY → max(reference × (1+offset))
              SELL → min(reference × (1+offset))
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import NamedTuple

from app.execution import ExecutionDomain
from app.strategy.base import RawSignal


@dataclass(frozen=True)
class TaggedSignal:
    """RawSignal 经过 instance 路由后带 account_group + signal_id 的版本。"""
    signal_id: str
    account_group: str
    raw: RawSignal
    execution_domain: ExecutionDomain = "paper"
    qmt_account_alias: str | None = None


@dataclass(frozen=True)
class AggregatedOrder:
    """归集后的订单。"""
    order_id: str
    account_group: str
    symbol: str
    direction: str
    quantity: int
    limit_price: float
    valid_date: str
    execution_domain: ExecutionDomain = "paper"
    qmt_account_alias: str | None = None


class OrderSignalMapping(NamedTuple):
    order_id: str
    signal_id: str
    signal_quantity: int


@dataclass
class AggregateResult:
    orders: list[AggregatedOrder] = field(default_factory=list)
    mappings: list[OrderSignalMapping] = field(default_factory=list)


class AggregateService:
    """信号归集。无 I/O，纯计算。"""

    @staticmethod
    def _signal_limit_price(s: RawSignal) -> float:
        return float(s.reference_price) * (1.0 + float(s.price_offset))

    def aggregate(
        self,
        signals: list[TaggedSignal],
        valid_date: str,
    ) -> AggregateResult:
        if not signals:
            return AggregateResult()

        groups: dict[
            tuple[ExecutionDomain, str | None, str, str, str], list[TaggedSignal]
        ] = defaultdict(list)
        for ts in signals:
            key = (
                ts.execution_domain,
                ts.qmt_account_alias,
                ts.account_group,
                ts.raw.symbol,
                ts.raw.direction,
            )
            groups[key].append(ts)

        result = AggregateResult()
        for (
            execution_domain, qmt_account_alias, account_group, symbol, direction,
        ), members in groups.items():
            order_id = uuid.uuid4().hex

            total_qty = sum(m.raw.quantity for m in members)
            prices = [self._signal_limit_price(m.raw) for m in members]
            if direction == "BUY":
                limit = max(prices)
            else:
                limit = min(prices)

            result.orders.append(AggregatedOrder(
                order_id=order_id,
                account_group=account_group,
                symbol=symbol,
                direction=direction,
                quantity=total_qty,
                limit_price=round(limit, 4),
                valid_date=valid_date,
                execution_domain=execution_domain,
                qmt_account_alias=qmt_account_alias,
            ))
            for m in members:
                result.mappings.append(OrderSignalMapping(
                    order_id=order_id,
                    signal_id=m.signal_id,
                    signal_quantity=m.raw.quantity,
                ))

        return result
