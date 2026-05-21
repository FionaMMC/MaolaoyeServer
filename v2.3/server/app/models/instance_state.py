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

    # 策略自身的持久状态（V20H: last_rb_idx / equity_history / daily_rets /
    # prev_hedge 等）。schema 由策略 adapter 自己定义，server 只负责存取。
    # nullable: 老数据迁移期为 None，adapter 首次读到 None 时按默认初始化。
    strategy_state: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
