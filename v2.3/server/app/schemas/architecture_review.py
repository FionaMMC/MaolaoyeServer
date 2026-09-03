"""Validated contracts for collaborative architecture-review notes."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


ReviewDecisionStatus = Literal[
    "pending", "confirmed", "change_required", "follow_up", "not_applicable",
]


class ArchitectureReviewCommentCreate(BaseModel):
    item_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    author: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=4000)

    @field_validator("author", "body")
    @classmethod
    def trim_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("内容不能为空")
        return cleaned


class ArchitectureReviewDecisionUpsert(BaseModel):
    status: ReviewDecisionStatus = "pending"
    rationale: str = Field(default="", max_length=4000)
    owner: str = Field(default="", max_length=80)
    updated_by: str = Field(min_length=1, max_length=80)

    @field_validator("rationale", "owner", "updated_by")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("updated_by")
    @classmethod
    def reviewer_required(cls, value: str) -> str:
        if not value:
            raise ValueError("记录人不能为空")
        return value


class ArchitectureReviewCommentItem(BaseModel):
    id: int
    item_id: str
    author: str
    body: str
    created_at: str


class ArchitectureReviewDecisionItem(BaseModel):
    item_id: str
    status: ReviewDecisionStatus
    rationale: str
    owner: str
    updated_by: str
    updated_at: str


class ArchitectureReviewSessionData(BaseModel):
    session_id: str
    comments: list[ArchitectureReviewCommentItem]
    decisions: list[ArchitectureReviewDecisionItem]
    updated_at: str | None = None
