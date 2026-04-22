"""trader_connector 测试：全程 mock xtquant.xttrader / xttype"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_xttrader(monkeypatch):
    trader_instance = MagicMock()
    trader_instance.start = MagicMock(return_value=None)
    trader_instance.connect = MagicMock(return_value=0)
    trader_instance.subscribe = MagicMock(return_value=0)

    XtQuantTraderCls = MagicMock(return_value=trader_instance)
    StockAccountCls = MagicMock(side_effect=lambda aid, t: SimpleNamespace(
        account_id=aid, account_type=t,
    ))

    fake_xttrader = SimpleNamespace(
        XtQuantTrader=XtQuantTraderCls,
        XtQuantTraderCallback=object,
    )
    fake_xttype = SimpleNamespace(StockAccount=StockAccountCls)
    fake_xtconstant = SimpleNamespace(
        STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11,
    )
    fake_xtdata = SimpleNamespace(data_dir="")

    pkg = SimpleNamespace(
        xttrader=fake_xttrader, xttype=fake_xttype,
        xtconstant=fake_xtconstant, xtdata=fake_xtdata,
    )
    for n, m in [
        ("xtquant", pkg),
        ("xtquant.xttrader", fake_xttrader),
        ("xtquant.xttype", fake_xttype),
        ("xtquant.xtconstant", fake_xtconstant),
        ("xtquant.xtdata", fake_xtdata),
    ]:
        monkeypatch.setitem(sys.modules, n, m)

    return SimpleNamespace(
        trader_instance=trader_instance,
        XtQuantTraderCls=XtQuantTraderCls,
        StockAccountCls=StockAccountCls,
    )


def test_connect_trader_returns_trader_and_account(fake_xttrader):
    from src.auction_order.trader_connector import connect_trader

    trader, account = connect_trader(
        data_dir="/tmp/fake_qmt", account_id="ACC123",
    )

    assert trader is fake_xttrader.trader_instance
    assert account.account_id == "ACC123"
    assert account.account_type == "STOCK"
    fake_xttrader.trader_instance.start.assert_called_once()
    fake_xttrader.trader_instance.connect.assert_called_once()
    fake_xttrader.trader_instance.subscribe.assert_called_once_with(account)


def test_connect_trader_connect_fail_raises(fake_xttrader):
    from src.auction_order.trader_connector import connect_trader

    fake_xttrader.trader_instance.connect.return_value = -1

    with pytest.raises(RuntimeError, match="connect"):
        connect_trader(data_dir="/tmp/fake_qmt", account_id="ACC")


def test_connect_trader_subscribe_fail_raises(fake_xttrader):
    from src.auction_order.trader_connector import connect_trader

    fake_xttrader.trader_instance.subscribe.return_value = -1

    with pytest.raises(RuntimeError, match="subscribe"):
        connect_trader(data_dir="/tmp/fake_qmt", account_id="ACC")


def test_connect_trader_empty_account_raises(fake_xttrader):
    from src.auction_order.trader_connector import connect_trader

    with pytest.raises(ValueError, match="account_id"):
        connect_trader(data_dir="/tmp/fake_qmt", account_id="")
