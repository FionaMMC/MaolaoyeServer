"""原始信号：策略输出，预检前后状态都在此表。"""
from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class RawSignal(Base):
    __tablename__ = "raw_signals"

    signal_id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_price: Mapped[float] = mapped_column(Float, nullable=False)
    price_offset: Mapped[float] = mapped_column(Float, nullable=False)
    limit_price: Mapped[float] = mapped_column(Float, nullable=False)
    valid_date: Mapped[str] = mapped_column(String, nullable=False, index=True)
    signal_time: Mapped[str] = mapped_column(String, nullable=False)
    precheck_status: Mapped[str] = mapped_column(String, nullable=False)
    precheck_reason: Mapped[str | None] = mapped_column(String, nullable=True)
