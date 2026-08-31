"""Materialized end-of-day portfolio risk facts.

One row is the reproducible risk view for one strategy instance and one date.
Benchmark series stay in Parquet and are joined at query time so the same
portfolio snapshot can be compared with different benchmarks without copying
portfolio facts.
"""
from __future__ import annotations

from sqlalchemy import Float, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class DailyRiskSnapshot(Base):
    __tablename__ = "daily_risk_snapshots"

    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    date: Mapped[str] = mapped_column(String, primary_key=True)
    execution_domain: Mapped[str] = mapped_column(
        String, nullable=False, default="paper", server_default="paper",
    )
    instance_kind: Mapped[str] = mapped_column(
        String, nullable=False, default="regular", server_default="regular",
    )

    nav: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    cash_source: Mapped[str] = mapped_column(String, nullable=False)
    long_market_value: Mapped[float] = mapped_column(Float, nullable=False)
    short_market_value: Mapped[float] = mapped_column(Float, nullable=False)
    gross_market_value: Mapped[float] = mapped_column(Float, nullable=False)
    net_market_value: Mapped[float] = mapped_column(Float, nullable=False)
    gross_exposure: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_exposure: Mapped[float | None] = mapped_column(Float, nullable=True)
    cash_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    holdings_count: Mapped[int] = mapped_column(Integer, nullable=False)
    priced_holdings_count: Mapped[int] = mapped_column(Integer, nullable=False)
    stale_mark_count: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_mark_count: Mapped[int] = mapped_column(Integer, nullable=False)
    pricing_coverage: Mapped[float] = mapped_column(Float, nullable=False)
    top_positions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    external_cash_flow: Mapped[float] = mapped_column(Float, nullable=False)
    cash_flow_status: Mapped[str] = mapped_column(String, nullable=False)
    portfolio_return: Mapped[float | None] = mapped_column(Float, nullable=True)

    calculation_version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
