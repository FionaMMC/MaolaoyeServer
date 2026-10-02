"""Durable, account-scoped manual emergency authorization and automation hold."""
from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class EmergencyExecution(Base):
    __tablename__ = "emergency_executions"

    authorization_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    request_sha256: Mapped[str] = mapped_column(String, nullable=False)
    authenticated_client: Mapped[str] = mapped_column(String, nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    release_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
