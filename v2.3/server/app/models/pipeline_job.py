"""Durable paper rebalance recovery; no broker submit or cash movement."""
from sqlalchemy import JSON, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class PipelineJob(Base):
    __tablename__ = "pipeline_jobs"

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_domain: Mapped[str] = mapped_column(String, nullable=False, default="paper")
    account_group: Mapped[str] = mapped_column(String, nullable=False)
    trade_date: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)
    next_attempt_at: Mapped[float] = mapped_column(Float, nullable=False)
    deadline: Mapped[float] = mapped_column(Float, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
