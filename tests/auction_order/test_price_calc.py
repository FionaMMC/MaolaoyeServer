"""price_calc 测试"""
from __future__ import annotations

import math

import pytest

from src.auction_order.price_calc import compute_submit_price


def test_buy_positive_offset():
    p = compute_submit_price(limit_price=1540.0, price_offset=0.005,
                              direction="BUY")
    assert math.isclose(p, 1547.70)


def test_sell_negative_offset():
    p = compute_submit_price(limit_price=10.0, price_offset=-0.005,
                              direction="SELL")
    assert math.isclose(p, 9.95)


def test_rounds_to_two_decimals():
    p = compute_submit_price(limit_price=1.234, price_offset=0.001,
                              direction="BUY")
    assert p == 1.24


def test_zero_offset_equals_limit():
    p = compute_submit_price(limit_price=20.0, price_offset=0.0, direction="BUY")
    assert p == 20.00


def test_buy_with_negative_offset_raises():
    with pytest.raises(ValueError, match="BUY"):
        compute_submit_price(limit_price=10.0, price_offset=-0.005,
                             direction="BUY")


def test_sell_with_positive_offset_raises():
    with pytest.raises(ValueError, match="SELL"):
        compute_submit_price(limit_price=10.0, price_offset=0.005,
                             direction="SELL")


def test_none_limit_price_raises():
    with pytest.raises(ValueError, match="limit_price"):
        compute_submit_price(limit_price=None, price_offset=0.005,
                             direction="BUY")
