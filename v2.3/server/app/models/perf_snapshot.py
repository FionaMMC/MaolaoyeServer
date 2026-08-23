"""每日策略实例净值快照。复合主键 (instance_id, date)。"""
from __future__ import annotations

from sqlalchemy import JSON, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class PerfSnapshot(Base):
    __tablename__ = "perf_snapshots"

    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    date: Mapped[str] = mapped_column(String, primary_key=True)
    execution_domain: Mapped[str] = mapped_column(
        String, nullable=False, default="paper", server_default="paper", index=True,
    )
    nav: Mapped[float] = mapped_column(Float, nullable=False)
    daily_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    positions_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
