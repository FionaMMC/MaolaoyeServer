"""Immutable operational closure receipts, separate from broker order status."""
from __future__ import annotations

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class HydraWorkflowClosure(Base):
    __tablename__ = "hydra_workflow_closures"

    receipt_id: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    execution_domain: Mapped[str] = mapped_column(String, nullable=False, index=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
