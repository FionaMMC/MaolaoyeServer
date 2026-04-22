"""validator 测试"""
from __future__ import annotations

from src.signal_query.validator import validate_signals


def _s(valid_date="20260422", **overrides) -> dict:
    d = {
        "signal_id": "sig1",
        "symbol": "600519.SH",
        "direction": "BUY",
        "quantity": 100,
        "order_type": "LIMIT",
        "limit_price": 1540.0,
        "price_offset": 0.005,
        "strategy_id": "s1",
        "signal_time": "2026-04-21T18:30:00+08:00",
        "valid_date": valid_date,
    }
    d.update(overrides)
    return d


def test_validate_all_pass():
    sigs = [_s(signal_id="a"), _s(signal_id="b")]
    valid, rejected = validate_signals(sigs, expected_date="20260422")
    assert len(valid) == 2
    assert rejected == []


def test_validate_rejects_mismatched_valid_date():
    sigs = [_s(signal_id="a", valid_date="20260423")]
    valid, rejected = validate_signals(sigs, expected_date="20260422")
    assert valid == []
    assert len(rejected) == 1
    assert rejected[0]["signal_id"] == "a"
    assert rejected[0]["_reason"].startswith("valid_date")


def test_validate_rejects_missing_field():
    sig = _s(signal_id="a")
    del sig["symbol"]
    valid, rejected = validate_signals([sig], expected_date="20260422")
    assert valid == []
    assert rejected[0]["_reason"].startswith("missing")


def test_validate_rejects_bad_direction():
    sigs = [_s(signal_id="a", direction="HOLD")]
    valid, rejected = validate_signals(sigs, expected_date="20260422")
    assert valid == []
    assert "direction" in rejected[0]["_reason"]


def test_validate_rejects_bad_order_type():
    sigs = [_s(signal_id="a", order_type="STOP")]
    valid, rejected = validate_signals(sigs, expected_date="20260422")
    assert valid == []
    assert "order_type" in rejected[0]["_reason"]


def test_validate_mixed_some_valid():
    sigs = [
        _s(signal_id="good", valid_date="20260422"),
        _s(signal_id="bad", valid_date="20260425"),
    ]
    valid, rejected = validate_signals(sigs, expected_date="20260422")
    assert [s["signal_id"] for s in valid] == ["good"]
    assert [s["signal_id"] for s in rejected] == ["bad"]
