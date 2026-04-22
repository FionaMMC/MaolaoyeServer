"""竞价报价计算：limit_price × (1 + price_offset)，两位小数。"""
from __future__ import annotations


def compute_submit_price(
    limit_price: float | None,
    price_offset: float,
    direction: str,
) -> float:
    if limit_price is None:
        raise ValueError("limit_price 不能为 None（MARKET 单由上游拒绝）")
    if direction == "BUY" and price_offset < 0:
        raise ValueError("BUY 方向要求 price_offset >= 0（超价买入），当前为负")
    if direction == "SELL" and price_offset > 0:
        raise ValueError("SELL 方向要求 price_offset <= 0（超价卖出为负），当前为正")

    raw = float(limit_price) * (1.0 + float(price_offset))
    return round(raw, 2)
