"""Order-ineligible shadow-strategy ledger tables.

These tables are deliberately separate from instance_state/raw_signals/orders/trades.
"""
from __future__ import annotations

from sqlalchemy import JSON, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ShadowInstanceState(Base):
    __tablename__ = "shadow_instance_state"

    shadow_id: Mapped[str] = mapped_column(String, primary_key=True)
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False)
    virtual_cash: Mapped[float] = mapped_column(Float, nullable=False)
    virtual_positions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String, nullable=False, default="waiting_target")
    decision_date: Mapped[str | None] = mapped_column(String, nullable=True)
    as_of_date: Mapped[str | None] = mapped_column(String, nullable=True)
    state_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    source_version: Mapped[str | None] = mapped_column(String, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    target_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    cumulative_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_turnover: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_update: Mapped[str] = mapped_column(String, nullable=False)


class ShadowTarget(Base):
    __tablename__ = "shadow_targets"

    shadow_id: Mapped[str] = mapped_column(String, primary_key=True)
    decision_date: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, primary_key=True)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    as_of_date: Mapped[str] = mapped_column(String, nullable=False)
    state_reason: Mapped[str] = mapped_column(String, nullable=False)
    source_version: Mapped[str] = mapped_column(String, nullable=False)
    input_hash: Mapped[str] = mapped_column(String, nullable=False)
    target_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class ShadowNavSnapshot(Base):
    __tablename__ = "shadow_nav_snapshots"

    shadow_id: Mapped[str] = mapped_column(String, primary_key=True)
    date: Mapped[str] = mapped_column(String, primary_key=True)
    nav: Mapped[float] = mapped_column(Float, nullable=False)
    daily_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    virtual_cash: Mapped[float] = mapped_column(Float, nullable=False)
    positions_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    transaction_cost: Mapped[float] = mapped_column(Float, nullable=False)
    turnover: Mapped[float] = mapped_column(Float, nullable=False)
    decision_date: Mapped[str | None] = mapped_column(String, nullable=True)
    as_of_date: Mapped[str | None] = mapped_column(String, nullable=True)
    state_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    source_version: Mapped[str | None] = mapped_column(String, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    target_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
