"""One atomic ownership projection per external income observation."""

from sqlalchemy import Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class IncomeAllocationReceipt(Base):
    __tablename__ = "income_allocation_receipts"

    observation_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String, nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
