"""ETF 分红、入金、出金等非交易现金流的幂等 journal。"""
from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class CashFlowJournal(Base):
    __tablename__ = "cash_flow_journal"
    __table_args__ = (
        UniqueConstraint(
            "execution_domain", "account_alias", "source", "source_event_id",
            name="uq_cash_flow_source_event",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_domain: Mapped[str] = mapped_column(String, nullable=False, index=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_date: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    qmt_cash_snapshot: Mapped[float | None] = mapped_column(Float, nullable=True)
    snapshot_time: Mapped[str | None] = mapped_column(String, nullable=True)
    transition_to_attributed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    currency: Mapped[str] = mapped_column(String, nullable=False, default="CNY")
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_event_id: Mapped[str] = mapped_column(String, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    applied_at: Mapped[str] = mapped_column(String, nullable=False)
