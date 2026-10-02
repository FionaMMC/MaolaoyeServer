"""Accounting components published atomically with a regular NAV snapshot.

An absent row means legacy NAV: never infer that newly registered rights were
already included in an old snapshot.
"""

from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class PerfValuation(Base):
    __tablename__ = "perf_valuations"

    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    date: Mapped[str] = mapped_column(String, primary_key=True)
    execution_domain: Mapped[str] = mapped_column(String, nullable=False)
    nav: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    dividend_receivable: Mapped[float] = mapped_column(Float, nullable=False)
