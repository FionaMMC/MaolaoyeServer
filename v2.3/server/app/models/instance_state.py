"""策略实例虚拟账本：每个 (account_group, strategy_id) 一行。"""
from __future__ import annotations

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class InstanceState(Base):
    __tablename__ = "instance_state"

    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    virtual_cash: Mapped[float] = mapped_column(nullable=False)
    virtual_positions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_update: Mapped[str] = mapped_column(String, nullable=False)
