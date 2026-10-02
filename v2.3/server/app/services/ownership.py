"""Instance symbol-ownership invariants.

Ownership is exclusive only inside the same physical-account scope.  A missing
account alias is treated as a conservative wildcard for legacy rows, while two
explicitly different aliases are independent accounts.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select

from app.models import InstanceState


class OwnershipOverlap(Exception):
    """Two instances claim the same symbol in the same account scope."""


def _overlap_is_attributed(first: InstanceState, second: InstanceState) -> bool:
    """Attributed ledgers may share a symbol; fill lineage preserves ownership."""
    return first.ledger_mode == second.ledger_mode == "attributed"


def _same_account_scope(first: InstanceState, second: InstanceState) -> bool:
    if first.execution_domain != second.execution_domain:
        return False
    if first.account_alias is None or second.account_alias is None:
        # A migrated legacy row has no trustworthy account identity.  Keep the
        # old fail-closed behaviour until it is assigned an explicit alias.
        return True
    return first.account_alias == second.account_alias


def validate_owned_symbol_rows(rows: Iterable[InstanceState]) -> None:
    """Raise when two rows claim a symbol in one physical-account scope."""
    owners_by_symbol: dict[str, list[InstanceState]] = {}
    for row in rows:
        for symbol in row.owned_symbols or ():
            for owner in owners_by_symbol.get(symbol, ()):
                if (
                    owner.instance_id != row.instance_id
                    and _same_account_scope(owner, row)
                    and not _overlap_is_attributed(owner, row)
                ):
                    alias = row.account_alias or owner.account_alias or "<legacy>"
                    raise OwnershipOverlap(
                        f"symbol {symbol} owned by both {owner.instance_id} and "
                        f"{row.instance_id} in {row.execution_domain}/{alias}"
                    )
            owners_by_symbol.setdefault(symbol, []).append(row)


def validate_no_owned_symbol_overlap(session) -> None:
    """Validate current and pending ``InstanceState`` rows in one transaction."""
    session.flush()
    rows = session.execute(select(InstanceState)).scalars().all()
    validate_owned_symbol_rows(rows)
