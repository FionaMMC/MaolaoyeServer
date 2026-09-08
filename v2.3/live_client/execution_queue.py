"""Offline cash readiness, distinct from a plan's projected sell proceeds.

The queue never creates/splits/reprices orders. Deferred orders have not entered
the broker call and may be resumed explicitly. Submitted/ambiguous buys retain
their full reservation until this attempt is closed, even if QMT cash lags.
"""
from __future__ import annotations

import math
import os
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path


def _money(value: float) -> Decimal:
    if not math.isfinite(float(value)) or float(value) < 0:
        raise ValueError("执行队列资金/成交价必须为非负有限数")
    return Decimal(str(value))


def cash_cents(value: float) -> int:
    return int((_money(value) * 100).to_integral_value(rounding=ROUND_FLOOR))


def fee_reserve(notional: Decimal, *, reserve_bps=10.0, min_commission=5.0) -> Decimal:
    """Reserve the greater of a rate and broker minimum, for each order."""
    return max(_money(min_commission), notional * _money(reserve_bps) / 10_000)


def buy_reservation_cents(order: dict, *, reserve_bps=10.0, min_commission=5.0) -> int:
    notional = _money(order["limit_price"]) * int(order["quantity"])
    return int(((notional + fee_reserve(
        notional, reserve_bps=reserve_bps, min_commission=min_commission,
    )) * 100).to_integral_value(rounding=ROUND_CEILING))


def cash_readiness(
    order: dict, *, initial_owned_cash: float, qmt_available_cash: float,
    submissions: list[dict], confirmed_sell_fills: dict[str, dict],
    reserve_bps: float = 10.0, min_commission: float = 5.0,
) -> dict:
    """Intersect actual QMT cash with owned cash + confirmed own net proceeds."""
    owned_cents = cash_cents(initial_owned_cash)
    proceeds_cents = reserved_cents = 0
    for row in submissions:
        if row["direction"] == "BUY" and row["submit_status"] in {
            "SUBMITTED", "SUBMITTING_UNKNOWN",
        }:
            reserved_cents += buy_reservation_cents(
                row, reserve_bps=reserve_bps, min_commission=min_commission,
            )
        elif row["direction"] == "SELL" and row["submit_status"] == "SUBMITTED":
            fill = confirmed_sell_fills.get(row["order_id"])
            if fill is None:
                continue
            quantity = fill["filled_quantity"]
            if (
                isinstance(quantity, bool) or not isinstance(quantity, int)
                or not 0 <= quantity <= int(row["quantity"])
            ):
                raise RuntimeError("QMT 已确认卖出成交量非法")
            price = _money(fill["filled_price"])
            if quantity and price <= 0:
                raise RuntimeError("QMT 已确认卖出成交价非法")
            if quantity:
                gross = price * quantity
                proceeds_cents += int(((gross - fee_reserve(
                    gross, reserve_bps=reserve_bps, min_commission=min_commission,
                )) * 100).to_integral_value(rounding=ROUND_FLOOR))
    remaining_cents = owned_cents + proceeds_cents - reserved_cents
    physical_cents = cash_cents(qmt_available_cash)
    required_cents = buy_reservation_cents(
        order, reserve_bps=reserve_bps, min_commission=min_commission,
    )
    return {
        "ready": required_cents <= min(remaining_cents, physical_cents),
        "required_cash": required_cents / 100,
        "owned_remaining_cash": remaining_cents / 100,
        "qmt_available_cash": physical_cents / 100,
        "confirmed_sell_proceeds": proceeds_cents / 100,
        "reserved_buy_cash": reserved_cents / 100,
    }


@contextmanager
def account_submission_lock(userdata_dir: Path, account_fingerprint: str):
    """One cooperating writer per QMT userdata/account, across strategy DBs.

    The OS releases the lock on process death. A lock file's existence is not
    ownership. This does not coordinate legacy clients or a second computer.
    """
    directory = Path(userdata_dir) / "hydra_execution_locks"
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f"{account_fingerprint}.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("该 QMT 账户已有离线提交进程，稍后继续队列") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
