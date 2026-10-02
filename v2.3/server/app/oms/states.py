"""Order state machine for the execution core: the only place that decides legality."""
from __future__ import annotations

from enum import Enum


class OrderState(str, Enum):
    PLANNED = "PLANNED"
    SUBMITTING = "SUBMITTING"
    ACKED = "ACKED"
    PARTIAL = "PARTIAL"
    PENDING_CANCEL = "PENDING_CANCEL"
    UNKNOWN = "UNKNOWN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED_DAY = "EXPIRED_DAY"
    NOT_SUBMITTED = "NOT_SUBMITTED"


S = OrderState
TERMINAL = frozenset({S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED_DAY, S.NOT_SUBMITTED})
# Any broker-observed state; an unknown submit can resolve to any of them.
_BROKER = frozenset({S.ACKED, S.PARTIAL, S.PENDING_CANCEL, S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED_DAY})
ALLOWED = {
    # Broker evidence wins: an order can appear at the broker before the agent's
    # SUBMIT_STARTED event reaches the server (server down, agent crash after submit).
    S.PLANNED: _BROKER | {S.SUBMITTING, S.NOT_SUBMITTED},
    S.SUBMITTING: _BROKER | {S.UNKNOWN, S.NOT_SUBMITTED},
    S.UNKNOWN: _BROKER | {S.NOT_SUBMITTED},
    S.ACKED: _BROKER,
    S.PARTIAL: _BROKER - {S.ACKED, S.REJECTED},
    S.PENDING_CANCEL: {S.PARTIAL, S.CANCELLED, S.FILLED, S.EXPIRED_DAY},
}

# QMT xtconstant order statuses; 51/52 (cancel requested) are deliberately not terminal.
_QMT = {48: S.ACKED, 49: S.ACKED, 50: S.ACKED, 55: S.PARTIAL, 51: S.PENDING_CANCEL, 52: S.PENDING_CANCEL,
        53: S.CANCELLED, 54: S.CANCELLED, 56: S.FILLED, 57: S.REJECTED}


class IllegalTransition(ValueError):
    pass


def transition(current: OrderState, new: OrderState) -> OrderState:
    """Return ``new`` if the move is legal; repeating the current state is a no-op."""
    if new is current:
        return current
    if current in TERMINAL or new not in ALLOWED.get(current, set()):
        raise IllegalTransition(f"{current.value} -> {new.value}")
    return new


def classify_qmt(status: int, traded: int, ordered: int) -> OrderState:
    if ordered > 0 and traded >= ordered:
        return S.FILLED
    return _QMT.get(int(status), S.UNKNOWN)


def legacy_status(state: OrderState, filled: int) -> str | None:
    """Map to the legacy TradeResult.status used by SettlementService; None = nothing to book yet."""
    if state is S.FILLED:
        return "FILLED"
    if state in (S.CANCELLED, S.EXPIRED_DAY):
        return "CANCELLED"
    if state is S.REJECTED:
        return "CANCELLED" if filled else "REJECTED"
    if state is S.NOT_SUBMITTED:
        return "NOT_SUBMITTED"
    return "PARTIAL" if filled > 0 else None
