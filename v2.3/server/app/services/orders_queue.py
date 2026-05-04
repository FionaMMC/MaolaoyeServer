"""订单队列：写归集结果 + 读 PENDING + 更新状态。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select

from app.models import Order, OrderSignalMap
from app.schemas.orders import OrderItem
from app.services.aggregate import AggregatedOrder, OrderSignalMapping


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class OrdersQueueService:
    """订单队列服务。每次调用通过 session_factory 拿独立 session。"""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def write_aggregated(
        self,
        orders: list[AggregatedOrder],
        mappings: Iterable[OrderSignalMapping],
    ) -> int:
        if not orders:
            return 0

        now = _now_iso()
        with self.session_factory() as session:
            for o in orders:
                session.add(Order(
                    order_id=o.order_id,
                    account_group=o.account_group,
                    symbol=o.symbol,
                    direction=o.direction,
                    quantity=o.quantity,
                    limit_price=o.limit_price,
                    valid_date=o.valid_date,
                    status="PENDING",
                    created_at=now,
                ))
            for m in mappings:
                session.add(OrderSignalMap(
                    order_id=m.order_id,
                    signal_id=m.signal_id,
                    signal_quantity=m.signal_quantity,
                ))
            session.commit()
        return len(orders)

    def list_pending(self, valid_date: str) -> list[OrderItem]:
        with self.session_factory() as session:
            stmt = (
                select(Order)
                .where(Order.valid_date == valid_date)
                .where(Order.status == "PENDING")
                .order_by(Order.created_at)
            )
            rows = session.execute(stmt).scalars().all()
            return [
                OrderItem(
                    order_id=r.order_id,
                    account_group=r.account_group,
                    symbol=r.symbol,
                    direction=r.direction,
                    quantity=r.quantity,
                    limit_price=r.limit_price,
                    valid_date=r.valid_date,
                )
                for r in rows
            ]

    def mark_status(self, order_id: str, status: str) -> bool:
        with self.session_factory() as session:
            order = session.get(Order, order_id)
            if order is None:
                return False
            order.status = status
            session.commit()
            return True
