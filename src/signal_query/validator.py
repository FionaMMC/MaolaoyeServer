"""信号校验：valid_date、必填字段、枚举值。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_REQUIRED = {
    "signal_id", "symbol", "direction", "quantity", "order_type",
    "price_offset", "strategy_id", "signal_time", "valid_date",
}
_DIRECTIONS = {"BUY", "SELL"}
_ORDER_TYPES = {"LIMIT", "MARKET"}


def validate_signals(
    signals: list[dict[str, Any]],
    expected_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (valid, rejected)。rejected 每条多一个 `_reason` 字段便于上报。"""
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for s in signals:
        missing = _REQUIRED - set(s.keys())
        if missing:
            rejected.append({**s, "_reason": f"missing fields: {sorted(missing)}"})
            continue
        if s["valid_date"] != expected_date:
            rejected.append({**s,
                             "_reason": f"valid_date {s['valid_date']} != "
                                        f"expected {expected_date}"})
            continue
        if s["direction"] not in _DIRECTIONS:
            rejected.append({**s, "_reason": f"bad direction: {s['direction']}"})
            continue
        if s["order_type"] not in _ORDER_TYPES:
            rejected.append({**s, "_reason": f"bad order_type: {s['order_type']}"})
            continue
        valid.append(s)

    if rejected:
        logger.warning("校验拒绝 %d 条信号：%s",
                       len(rejected),
                       [(r["signal_id"], r["_reason"]) for r in rejected
                        if "signal_id" in r])
    return valid, rejected
