"""Shared meeting notes for the server architecture/risk review dashboard."""
from __future__ import annotations

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ArchitectureReviewComment(Base):
    """Append-only comment attached to one stable review-catalog item."""

    __tablename__ = "architecture_review_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    author: Mapped[str] = mapped_column(String(80), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ArchitectureReviewDecision(Base):
    """Latest shared decision for one catalog item in one review session."""

    __tablename__ = "architecture_review_decisions"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "item_id", name="uq_architecture_review_session_item",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    owner: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(String(80), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
