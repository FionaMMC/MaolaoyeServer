"""归集订单 → 原始信号映射（拆单按比例分摊用）。复合主键。"""
from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class OrderSignalMap(Base):
    __tablename__ = "order_signal_map"

    order_id: Mapped[str] = mapped_column(String, primary_key=True)
    signal_id: Mapped[str] = mapped_column(String, primary_key=True)
    signal_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
