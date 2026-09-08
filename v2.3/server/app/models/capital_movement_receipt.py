"""Immutable request evidence for additive strategy capital commands."""
from sqlalchemy import Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class CapitalMovementReceipt(Base):
    __tablename__ = "capital_movement_receipts"

    # Same transaction as CashFlowJournal; no existing table column migration.
    journal_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String, nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    calculation_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
