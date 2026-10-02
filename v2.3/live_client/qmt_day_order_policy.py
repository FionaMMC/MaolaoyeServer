"""The approved Hydra broker day-order expiry convention, not QMT raw status.

QMT_DAY_ORDER_1500_V1 is deliberately narrow: only raw ORDER_REPORTED (50)
observed on that same Chinese trading day at/after 15:00 is policy-expired.
It never invents a missing order, fills, cash, or a broker cancellation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


CHINA_TIMEZONE = timezone(timedelta(hours=8))
EXPIRATION_POLICY_ID = "QMT_DAY_ORDER_1500_V1"


def _date_from_broker_order_time(value: int | None) -> str | None:
    """Use a full broker date when present; legacy HHMMSS carries no date.

    Current-day query semantics plus the frozen valid_date are the date proof
    for legacy time-only/missing fields. Full timestamps supply an additional
    consistency check, so a reused numeric broker id cannot override the date.
    """
    if value is None or value == 0:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("QMT order_time 格式非法")
    if value <= 235959:
        datetime.strptime(f"{value:06d}", "%H%M%S")
        return None
    digits = str(value)
    if len(digits) in {8, 14} and digits.startswith("20"):
        fmt = "%Y%m%d" if len(digits) == 8 else "%Y%m%d%H%M%S"
        return datetime.strptime(digits, fmt).strftime("%Y%m%d")
    seconds = value / 1000 if value >= 100_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, tz=CHINA_TIMEZONE).strftime("%Y%m%d")
    except (OSError, OverflowError) as exc:
        raise ValueError("QMT order_time 日期不可验证") from exc


def day_order_expiration(
    *, qmt_status: int, filled_quantity: int, ordered_quantity: int,
    valid_date: str, observed_at: datetime, broker_order_time: int | None = None,
) -> dict | None:
    """Return an effective terminal policy observation, or no applicable policy.

    The caller must first match the account, broker id, remark, symbol,
    direction and original quantity against the frozen Hydra order. This helper
    is not an identity check and is not a claim of a broker-native terminal.
    """
    if qmt_status != 50:
        return None
    if (
        isinstance(filled_quantity, bool) or not isinstance(filled_quantity, int)
        or isinstance(ordered_quantity, bool) or not isinstance(ordered_quantity, int)
        or ordered_quantity <= 0 or not 0 <= filled_quantity <= ordered_quantity
    ):
        raise ValueError("QMT 累计成交数量非法")
    if filled_quantity == ordered_quantity:
        return None  # Actual complete fills always win over the expiry policy.
    if observed_at.utcoffset() is None:
        raise ValueError("QMT 状态观察时间必须带时区")
    if len(valid_date) != 8 or not valid_date.isdigit():
        return None  # No expiry date proof; still allow genuine fills upstream.
    try:
        datetime.strptime(valid_date, "%Y%m%d")
    except ValueError:
        return None
    china_observed = observed_at.astimezone(CHINA_TIMEZONE)
    if china_observed.strftime("%Y%m%d") != valid_date:
        # query_stock_orders returns today's orders. Never reinterpret a prior
        # day's order using a next-day response with a reused numeric id.
        return None
    try:
        broker_date = _date_from_broker_order_time(broker_order_time)
    except ValueError:
        return None
    if broker_date is not None and broker_date != valid_date:
        return None
    cutoff = china_observed.replace(hour=15, minute=0, second=0, microsecond=0)
    if china_observed < cutoff:
        return None
    return {
        "status": "EXPIRED_BY_POLICY",
        "raw_qmt_status": qmt_status,
        "status_observed_at": china_observed.isoformat(),
        "expiration_policy_id": EXPIRATION_POLICY_ID,
    }
