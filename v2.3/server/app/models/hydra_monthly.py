"""Immutable monthly inputs and the server-computed execution-plan receipt."""

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class HydraMonthlyCycle(Base):
    __tablename__ = "hydra_monthly_cycles"
    cycle_id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_domain: Mapped[str] = mapped_column(String, index=True)
    account_alias: Mapped[str] = mapped_column(String, index=True)
    instance_id: Mapped[str] = mapped_column(String, index=True)
    as_of_date: Mapped[str] = mapped_column(String, index=True)
    input_hashes: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, index=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String)
