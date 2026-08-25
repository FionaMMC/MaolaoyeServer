"""Hydra 月度目标、调仓与逐日尝试的不可变审计骨架。"""
from __future__ import annotations

from sqlalchemy import JSON, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class HydraTarget(Base):
    __tablename__ = "hydra_targets"
    __table_args__ = (
        UniqueConstraint(
            "execution_domain", "account_alias", "basket_sha256",
            name="uq_hydra_target_domain_account_basket",
        ),
    )

    target_id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_domain: Mapped[str] = mapped_column(String, nullable=False, index=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    strategy_version: Mapped[str] = mapped_column(String, nullable=False)
    publisher_source_commit: Mapped[str] = mapped_column(String, nullable=False)
    decision_date: Mapped[str] = mapped_column(String, nullable=False, index=True)
    as_of_date: Mapped[str] = mapped_column(String, nullable=False)
    execution_date: Mapped[str] = mapped_column(String, nullable=False, index=True)
    basket_sha256: Mapped[str] = mapped_column(String, nullable=False, index=True)
    research_input_hashes: Mapped[dict] = mapped_column(JSON, nullable=False)
    input_hashes: Mapped[dict] = mapped_column(JSON, nullable=False)
    weights: Mapped[dict] = mapped_column(JSON, nullable=False)
    cash_buffer_weight: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class HydraRebalance(Base):
    __tablename__ = "hydra_rebalances"

    rebalance_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    execution_domain: Mapped[str] = mapped_column(String, nullable=False, index=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    baseline_cash: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_positions: Mapped[dict] = mapped_column(JSON, nullable=False)
    target_shares: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reconciliation_status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class HydraExecutionAttempt(Base):
    __tablename__ = "hydra_execution_attempts"
    __table_args__ = (
        UniqueConstraint(
            "rebalance_id", "attempt_number", name="uq_hydra_rebalance_attempt_number",
        ),
        UniqueConstraint(
            "execution_domain", "batch_sha256", name="uq_hydra_attempt_domain_batch",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String, primary_key=True)
    rebalance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    execution_domain: Mapped[str] = mapped_column(String, nullable=False, index=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_date: Mapped[str] = mapped_column(String, nullable=False, index=True)
    residual_before: Mapped[dict] = mapped_column(JSON, nullable=False)
    residual_after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pretrade_reconciliation_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    posttrade_reconciliation_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    reconciled_cash: Mapped[float | None] = mapped_column(Float, nullable=True)
    reconciled_positions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    risk_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    batch_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    batch_sha256: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)
