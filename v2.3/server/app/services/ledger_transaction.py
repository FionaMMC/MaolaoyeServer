"""Short monetary-write transaction boundary; never holds while awaiting QMT.

SQLite is the deployed storage. BEGIN IMMEDIATE serializes participating writers
before they read balances. Other dialects lock the existing account rows; a
dedicated account row and migration of *all* ledger writers are still needed for
an all-writer, multi-process account transaction contract.
"""
from sqlalchemy import or_, select, text

from app.models import InstanceState


def begin_ledger_transaction(session, execution_domain: str, account_alias: str | None) -> None:
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
        return
    query = (
        select(InstanceState.instance_id)
        .where(InstanceState.execution_domain == execution_domain)
    )
    if account_alias is not None:
        query = query.where(or_(
            InstanceState.account_alias == account_alias,
            InstanceState.account_alias.is_(None),
        ))
    session.execute(query
        .order_by(InstanceState.instance_id)
        .with_for_update()
    ).all()
