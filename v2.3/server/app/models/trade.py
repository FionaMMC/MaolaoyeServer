"""成交回报。"""
from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_domain: Mapped[str] = mapped_column(
        String, nullable=False, default="paper", server_default="paper", index=True,
    )
    order_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_price: Mapped[float] = mapped_column(Float, nullable=False)
    filled_time: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[str] = mapped_column(String, nullable=False)
