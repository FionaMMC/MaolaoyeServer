"""订单队列：写归集结果 + 读 PENDING + 更新状态。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import case, select

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
                    execution_domain=o.execution_domain,
                    qmt_account_alias=o.qmt_account_alias,
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

    def list_pending(
        self,
        valid_date: str,
        execution_domain: str = "paper",
        allowed_account_aliases: tuple[str, ...] | None = None,
    ) -> list[OrderItem]:
        with self.session_factory() as session:
            stmt = (
                select(Order)
                .where(Order.valid_date == valid_date)
                .where(Order.execution_domain == execution_domain)
                .where(Order.status == "PENDING")
                .order_by(
                    case((Order.direction == "SELL", 0), else_=1),
                    Order.created_at,
                    Order.order_id,
                )
            )
            if allowed_account_aliases:
                stmt = stmt.where(Order.qmt_account_alias.in_(allowed_account_aliases))
            rows = session.execute(stmt).scalars().all()
            # 拉取即盖章（只记首次）：fetched_at 非空的日期不允许默认重算，
            # 否则 order_id 换新 → 客户端次日成交回报全量 unmatched（2026-07-02 事故）。
            now = _now_iso()
            stamped = False
            for r in rows:
                if r.fetched_at is None:
                    r.fetched_at = now
                    stamped = True
            if stamped:
                session.commit()
            return [
                OrderItem(
                    order_id=r.order_id,
                    execution_domain=r.execution_domain,
                    qmt_account_alias=r.qmt_account_alias,
                    target_id=r.target_id,
                    rebalance_id=r.rebalance_id,
                    attempt_id=r.attempt_id,
                    attempt_number=r.attempt_number,
                    batch_id=r.batch_id,
                    batch_sha256=r.batch_sha256,
                    target_hash=r.target_hash,
                    execution_reference_price=r.execution_reference_price,
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
