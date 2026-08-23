"""每笔订单的决策跳空、执行损耗、ETF 溢价与费用。"""
from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ExecutionQualityObservation(Base):
    __tablename__ = "execution_quality"

    order_id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_domain: Mapped[str] = mapped_column(String, nullable=False, index=True)
    target_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    rebalance_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    attempt_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    decision_reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    arrival_reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    arrival_reference_time: Mapped[str | None] = mapped_column(String, nullable=True)
    submitted_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    submitted_time: Mapped[str | None] = mapped_column(String, nullable=True)
    qmt_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fill_vwap: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    iopv: Mapped[float | None] = mapped_column(Float, nullable=True)
    iopv_time: Mapped[str | None] = mapped_column(String, nullable=True)
    decision_gap_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    execution_shortfall_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    premium_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_fees: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
