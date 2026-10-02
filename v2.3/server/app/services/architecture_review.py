"""Persistence service for collaborative architecture-review notes."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.models import ArchitectureReviewComment, ArchitectureReviewDecision
from app.schemas.architecture_review import (
    ArchitectureReviewCommentCreate,
    ArchitectureReviewCommentItem,
    ArchitectureReviewDecisionItem,
    ArchitectureReviewDecisionUpsert,
    ArchitectureReviewSessionData,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ArchitectureReviewService:
    """Small, DB-backed collaboration surface; catalog content remains read-only."""

    def __init__(self, session_factory, *, session_id: str, allowed_item_ids: set[str]):
        self.session_factory = session_factory
        self.session_id = session_id
        self.allowed_item_ids = set(allowed_item_ids)

    def _require_item(self, item_id: str) -> None:
        if item_id not in self.allowed_item_ids:
            raise ValueError(f"未知审阅项: {item_id}")

    def snapshot(self) -> ArchitectureReviewSessionData:
        with self.session_factory() as session:
            comments = session.execute(
                select(ArchitectureReviewComment)
                .where(ArchitectureReviewComment.session_id == self.session_id)
                .order_by(
                    ArchitectureReviewComment.created_at,
                    ArchitectureReviewComment.id,
                )
            ).scalars().all()
            decisions = session.execute(
                select(ArchitectureReviewDecision)
                .where(ArchitectureReviewDecision.session_id == self.session_id)
                .order_by(ArchitectureReviewDecision.item_id)
            ).scalars().all()

        timestamps = [row.created_at for row in comments] + [
            row.updated_at for row in decisions
        ]
        return ArchitectureReviewSessionData(
            session_id=self.session_id,
            comments=[
                ArchitectureReviewCommentItem(
                    id=row.id,
                    item_id=row.item_id,
                    author=row.author,
                    body=row.body,
                    created_at=row.created_at,
                )
                for row in comments
            ],
            decisions=[
                ArchitectureReviewDecisionItem(
                    item_id=row.item_id,
                    status=row.status,
                    rationale=row.rationale,
                    owner=row.owner,
                    updated_by=row.updated_by,
                    updated_at=row.updated_at,
                )
                for row in decisions
            ],
            updated_at=max(timestamps) if timestamps else None,
        )

    def add_comment(
        self, payload: ArchitectureReviewCommentCreate,
    ) -> ArchitectureReviewCommentItem:
        self._require_item(payload.item_id)
        row = ArchitectureReviewComment(
            session_id=self.session_id,
            item_id=payload.item_id,
            author=payload.author,
            body=payload.body,
            created_at=_now_iso(),
        )
        with self.session_factory() as session:
            session.add(row)
            session.flush()
            result = ArchitectureReviewCommentItem(
                id=row.id,
                item_id=row.item_id,
                author=row.author,
                body=row.body,
                created_at=row.created_at,
            )
            session.commit()
        return result

    def upsert_decision(
        self, item_id: str, payload: ArchitectureReviewDecisionUpsert,
    ) -> ArchitectureReviewDecisionItem:
        self._require_item(item_id)
        now = _now_iso()
        with self.session_factory() as session:
            row = session.execute(
                select(ArchitectureReviewDecision).where(
                    ArchitectureReviewDecision.session_id == self.session_id,
                    ArchitectureReviewDecision.item_id == item_id,
                )
            ).scalar_one_or_none()
            if row is None:
                row = ArchitectureReviewDecision(
                    session_id=self.session_id,
                    item_id=item_id,
                    status=payload.status,
                    rationale=payload.rationale,
                    owner=payload.owner,
                    updated_by=payload.updated_by,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.status = payload.status
                row.rationale = payload.rationale
                row.owner = payload.owner
                row.updated_by = payload.updated_by
                row.updated_at = now
            session.flush()
            result = ArchitectureReviewDecisionItem(
                item_id=row.item_id,
                status=row.status,
                rationale=row.rationale,
                owner=row.owner,
                updated_by=row.updated_by,
                updated_at=row.updated_at,
            )
            session.commit()
        return result
