"""归集后的订单：本地 client 据此下 QMT 委托。"""
from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_group: Mapped[str] = mapped_column(String, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[float] = mapped_column(Float, nullable=False)
    valid_date: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    # 真账户 FILL 了但虚拟账本对账失败（cash 穿仓或 position 不足）
    # True 时需要人工对账：QMT 真实仓位/现金 ↔ instance_state
    bookkeeping_divergence: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )

    # 客户端首次通过 GET /orders 拉取本单的时间（2026-07-02 事故护栏）。
    # 非空 = 客户端可能已按本 order_id 下单；重算该日会换 ID → 成交回报 unmatched，
    # 所以 pipeline 对含已拉取订单的日期默认拒绝重算（需显式 force）。
    fetched_at: Mapped[str | None] = mapped_column(String, nullable=True)
