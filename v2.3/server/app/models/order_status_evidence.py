"""Append-only broker observation evidence; effective expiry is not raw rejection."""
from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class OrderStatusEvidence(Base):
    __tablename__ = "order_status_evidence"

    evidence_sha256: Mapped[str] = mapped_column(String, primary_key=True)
    order_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    execution_domain: Mapped[str] = mapped_column(String, nullable=False)
    account_alias: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    received_at: Mapped[str] = mapped_column(String, nullable=False)
