"""Durable approved requests waiting for fresh adjacent-day execution data."""

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class HydraExecutionPlan(Base):
    __tablename__ = "hydra_execution_plans"
    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_domain: Mapped[str] = mapped_column(String, index=True)
    account_alias: Mapped[str] = mapped_column(String, index=True)
    instance_id: Mapped[str] = mapped_column(String, index=True)
    request_payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, index=True)
    response_payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String)
