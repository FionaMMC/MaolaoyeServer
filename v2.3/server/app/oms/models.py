"""Execution-core tables. Only app.oms.ledger and app.oms.cycles write them."""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base

__all__ = ["OmsTargetVersion", "OmsCycle", "OmsSession", "OmsOrder", "OmsOrderEvent", "OmsFill",
           "OmsBrokerSnapshot", "OmsReconciliation"]


class OmsTargetVersion(Base):
    __tablename__ = "oms_target_versions"
    __table_args__ = (UniqueConstraint("instance_id", "signal_date", "source_sha256"),)
    target_version_id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    signal_date: Mapped[str] = mapped_column(String, nullable=False)
    weights: Mapped[dict] = mapped_column(JSON, nullable=False)
    signal_closes: Mapped[dict] = mapped_column(JSON, nullable=False)
    calendar: Mapped[list] = mapped_column(JSON, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class OmsCycle(Base):
    __tablename__ = "oms_cycles"
    cycle_id: Mapped[str] = mapped_column(String, primary_key=True)
    cycle_no: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    target_version_id: Mapped[str] = mapped_column(String, nullable=False)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False)
    policy: Mapped[dict] = mapped_column(JSON, nullable=False)
    nav_at_signal: Mapped[float] = mapped_column(Float, nullable=False)
    frozen_target: Mapped[dict] = mapped_column(JSON, nullable=False)
    sell_anchor: Mapped[dict] = mapped_column(JSON, nullable=False)
    buy_anchor: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    lot_gap: Mapped[float] = mapped_column(Float, nullable=False)
    schedule: Mapped[list] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    close_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class OmsSession(Base):
    __tablename__ = "oms_sessions"
    __table_args__ = (UniqueConstraint("cycle_id", "seq"),)
    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_date: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    sell_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    plan_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    basis_snapshot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    deferrals: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class OmsOrder(Base):
    __tablename__ = "oms_orders"
    client_order_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    cycle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    trade_date: Mapped[str] = mapped_column(String, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[float] = mapped_column(Float, nullable=False)
    reference_price: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    filled_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.)
    projected_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projected_status: Mapped[str | None] = mapped_column(String, nullable=True)
    last_observed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class OmsOrderEvent(Base):
    __tablename__ = "oms_order_events"
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[str] = mapped_column(String, nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)


class OmsFill(Base):
    __tablename__ = "oms_fills"
    __table_args__ = (UniqueConstraint("account_alias", "broker_trade_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False)
    broker_trade_id: Mapped[str] = mapped_column(String, nullable=False)
    client_order_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    traded_at: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[str] = mapped_column(String, nullable=False)


class OmsBrokerSnapshot(Base):
    __tablename__ = "oms_broker_snapshots"
    snapshot_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    trade_date: Mapped[str] = mapped_column(String, nullable=False)
    taken_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    received_at: Mapped[str] = mapped_column(String, nullable=False)


class OmsReconciliation(Base):
    __tablename__ = "oms_reconciliations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    cycle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    discrepancies: Mapped[list] = mapped_column(JSON, nullable=False)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
