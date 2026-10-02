"""Non-spendable dividend rights, separate from received cash."""

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class DividendEntitlement(Base):
    __tablename__ = "dividend_entitlements"
    __table_args__ = (
        UniqueConstraint("execution_domain", "instance_id", "symbol", "record_date", "ex_date"),
        UniqueConstraint(
            "execution_domain", "account_alias", "settlement_source", "settlement_event_id"
        ),
    )
    entitlement_id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_domain: Mapped[str] = mapped_column(String, nullable=False)
    account_alias: Mapped[str] = mapped_column(String, nullable=False)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    record_date: Mapped[str] = mapped_column(String, nullable=False)
    ex_date: Mapped[str] = mapped_column(String, nullable=False)
    pay_date: Mapped[str] = mapped_column(String, nullable=False)
    entitled_quantity: Mapped[str] = mapped_column(String, nullable=False)
    cash_per_share: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[str] = mapped_column(String, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String, nullable=False)
    settlement_source: Mapped[str] = mapped_column(String, nullable=False)
    settlement_event_id: Mapped[str] = mapped_column(String, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String, nullable=False)
