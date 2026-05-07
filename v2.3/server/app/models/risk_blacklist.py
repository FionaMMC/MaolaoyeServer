"""持久风险黑名单：手工维护的 symbol 列表，不受 clear-state 影响。

跟 BlacklistService 从 REJECTED orders 自动派生的列表互补——
自动派生那部分会被 clear-state 清掉，这张表是永久记录。
"""
from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class RiskBlacklistEntry(Base):
    __tablename__ = "risk_blacklist"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    reason: Mapped[str] = mapped_column(String, nullable=False, default="")
    added_at: Mapped[str] = mapped_column(String, nullable=False)
