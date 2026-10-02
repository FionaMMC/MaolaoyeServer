"""Account-scoped append-only publication pointers to immutable execution data."""

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class HydraExecutionPublication(Base):
    __tablename__ = "hydra_execution_publications"
    publication_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_alias: Mapped[str] = mapped_column(String, index=True)
    reference_date: Mapped[str] = mapped_column(String, index=True)
    execution_raw_sha256: Mapped[str] = mapped_column(String)
    execution_calendar_sha256: Mapped[str] = mapped_column(String)
    producer_commit: Mapped[str] = mapped_column(String)
    observed_at: Mapped[str] = mapped_column(String)
    response_payload: Mapped[dict] = mapped_column(JSON)
