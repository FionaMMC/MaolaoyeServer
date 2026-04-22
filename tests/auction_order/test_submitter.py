"""submitter 测试"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.auction_order.signal_reader import Signal


@pytest.fixture
def fake_xtconstant(monkeypatch):
    const = SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11)
    monkeypatch.setitem(sys.modules, "xtquant.xtconstant", const)
    monkeypatch.setitem(sys.modules, "xtquant",
                        SimpleNamespace(xtconstant=const))
    return const


def _sig(**overrides) -> Signal:
    kw = dict(
        signal_id="sig1", symbol="600519.SH", direction="BUY",
        quantity=100, order_type="LIMIT",
        limit_price=1540.0, price_offset=0.005, strategy_id="s",
        signal_time="2026-04-21T18:30:00+08:00", valid_date="20260422",
    )
    kw.update(overrides)
    return Signal(**kw)


def test_submit_happy_path_buy(fake_xtconstant):
    from src.auction_order.submitter import submit_order

    trader = MagicMock()
    trader.order_stock = MagicMock(return_value=123456)
    account = SimpleNamespace(account_id="ACC")

    r = submit_order(trader, account, _sig())

    assert r.ok is True
    assert r.order_id == "123456"
    assert r.submitted_price == 1547.70
    assert r.submitted_quantity == 100
    assert r.submitted_at is not None
    assert r.error is None

    call_args_flat = list(trader.order_stock.call_args.args) + \
                     list(trader.order_stock.call_args.kwargs.values())
    assert fake_xtconstant.FIX_PRICE in call_args_flat
    assert fake_xtconstant.STOCK_BUY in call_args_flat


def test_submit_happy_path_sell(fake_xtconstant):
    from src.auction_order.submitter import submit_order

    trader = MagicMock()
    trader.order_stock = MagicMock(return_value=789)
    account = SimpleNamespace(account_id="ACC")

    r = submit_order(trader, account,
                     _sig(direction="SELL", price_offset=-0.005))

    assert r.ok is True
    assert r.submitted_price == 1532.30  # 1540 * 0.995
    call_args_flat = list(trader.order_stock.call_args.args) + \
                     list(trader.order_stock.call_args.kwargs.values())
    assert fake_xtconstant.STOCK_SELL in call_args_flat


def test_submit_rejects_market_order_type(fake_xtconstant):
    from src.auction_order.submitter import submit_order

    trader = MagicMock()
    account = SimpleNamespace(account_id="ACC")

    r = submit_order(trader, account,
                     _sig(order_type="MARKET", limit_price=None))

    assert r.ok is False
    assert "MARKET" in (r.error or "")
    trader.order_stock.assert_not_called()


def test_submit_negative_order_id_is_failure(fake_xtconstant):
    from src.auction_order.submitter import submit_order

    trader = MagicMock()
    trader.order_stock = MagicMock(return_value=-1)
    account = SimpleNamespace(account_id="ACC")

    r = submit_order(trader, account, _sig())

    assert r.ok is False
    assert r.order_id is None
    assert "-1" in (r.error or "")


def test_submit_exception_captured(fake_xtconstant):
    from src.auction_order.submitter import submit_order

    trader = MagicMock()
    trader.order_stock = MagicMock(side_effect=RuntimeError("boom"))
    account = SimpleNamespace(account_id="ACC")

    r = submit_order(trader, account, _sig())

    assert r.ok is False
    assert "boom" in (r.error or "")


def test_submit_price_calculation_error(fake_xtconstant):
    from src.auction_order.submitter import submit_order

    trader = MagicMock()
    account = SimpleNamespace(account_id="ACC")

    r = submit_order(trader, account,
                     _sig(direction="BUY", price_offset=-0.005))

    assert r.ok is False
    assert "price_offset" in (r.error or "") or "BUY" in (r.error or "")
    trader.order_stock.assert_not_called()
