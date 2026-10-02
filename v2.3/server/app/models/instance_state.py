"""策略实例虚拟账本：每个 (account_group, strategy_id) 一行。"""
from __future__ import annotations

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class InstanceState(Base):
    __tablename__ = "instance_state"

    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_domain: Mapped[str] = mapped_column(
        String, nullable=False, default="paper", server_default="paper", index=True,
    )
    account_alias: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # 物理账户与策略账本的关系：legacy=迁移前语义；dedicated=独占账户；
    # attributed=共享物理账户，只能使用归属到本实例的现金与持仓。
    ledger_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="legacy", server_default="legacy", index=True,
    )
    virtual_cash: Mapped[float] = mapped_column(nullable=False)
    virtual_positions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_update: Mapped[str] = mapped_column(String, nullable=False)

    # 策略自身的持久状态（V20H: last_rb_idx / equity_history / daily_rets /
    # prev_hedge 等）。schema 由策略 adapter 自己定义，server 只负责存取。
    # nullable: 老数据迁移期为 None，adapter 首次读到 None 时按默认初始化。
    strategy_state: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # 该 instance "拥有" 的标的白名单（multi-instance 共享 QMT 账户时用）。
    # None 表示 legacy 模式：reconcile 看到的 positions = "全部 - 其他 instance 的 owned"。
    # 列表表示严格白名单：reconcile 只对账列表内 symbol。
    owned_symbols: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
