"""next_trading_day 测试：mock xtquant"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_xtdata(monkeypatch):
    fake = SimpleNamespace(
        data_dir="",
        get_trading_dates=MagicMock(return_value=[
            "20260420", "20260421", "20260422",
            "20260423", "20260424", "20260427",
        ]),
    )
    monkeypatch.setitem(sys.modules, "xtquant", SimpleNamespace(xtdata=fake))
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake)
    return fake


def test_next_trading_day_skips_weekend(fake_xtdata):
    from src.signal_query.next_trading_day import next_trading_day

    result = next_trading_day("20260424")
    assert result == "20260427"


def test_next_trading_day_next_day_is_trading(fake_xtdata):
    from src.signal_query.next_trading_day import next_trading_day

    result = next_trading_day("20260422")
    assert result == "20260423"


def test_next_trading_day_today_not_in_calendar_raises(fake_xtdata):
    from src.signal_query.next_trading_day import next_trading_day

    with pytest.raises(ValueError, match="today"):
        next_trading_day("20260425")


def test_next_trading_day_no_future_date_raises(fake_xtdata):
    from src.signal_query.next_trading_day import next_trading_day

    fake_xtdata.get_trading_dates.return_value = ["20260420", "20260421", "20260422"]

    with pytest.raises(RuntimeError, match="下一"):
        next_trading_day("20260422")
