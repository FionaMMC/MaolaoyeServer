"""connector 测试：mock xtquant"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_xtquant(monkeypatch):
    fake_xtdata = SimpleNamespace(
        data_dir="",
        get_trading_dates=MagicMock(return_value=[]),
    )
    fake_pkg = SimpleNamespace(xtdata=fake_xtdata)
    monkeypatch.setitem(sys.modules, "xtquant", fake_pkg)
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake_xtdata)
    return fake_xtdata


def test_init_xtquant_sets_data_dir(fake_xtquant):
    from src.market_data_download.connector import init_xtquant

    init_xtquant(data_dir="/tmp/fake_qmt")
    assert fake_xtquant.data_dir == "/tmp/fake_qmt"


def test_init_xtquant_empty_data_dir_raises(fake_xtquant):
    from src.market_data_download.connector import init_xtquant

    with pytest.raises(ValueError, match="data_dir"):
        init_xtquant(data_dir="")


def test_startup_check_ok(fake_xtquant):
    from src.market_data_download.connector import startup_check

    fake_xtquant.get_trading_dates.return_value = [
        "20260420", "20260421", "20260422",
    ]

    startup_check(data_dir="/tmp/fake_qmt")
    assert fake_xtquant.data_dir == "/tmp/fake_qmt"


def test_startup_check_trading_dates_empty_raises(fake_xtquant):
    from src.market_data_download.connector import startup_check

    fake_xtquant.get_trading_dates.return_value = []

    with pytest.raises(RuntimeError, match="QMT"):
        startup_check(data_dir="/tmp/fake_qmt")
