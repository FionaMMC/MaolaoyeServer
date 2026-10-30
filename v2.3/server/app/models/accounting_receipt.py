"""Immutable evidence and results for explicit monetary/corporate-action operations."""
from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class AccountingReceipt(Base):
    __tablename__ = "accounting_receipts"
    __table_args__ = (UniqueConstraint("execution_domain", "account_alias", "kind", "event_id",
                                      name="uq_accounting_operation"),)
    receipt_id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_domain: Mapped[str] = mapped_column(String, nullable=False, index=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    event_id: Mapped[str] = mapped_column(String, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String, nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
