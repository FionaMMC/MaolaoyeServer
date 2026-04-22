"""downloader 测试：mock xtquant"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest


def _fake_market_data_frame(symbols: list[str], date: str, fields: list[str]) -> dict:
    """模拟 xtdata.get_market_data 的返回格式：{field: DataFrame(index=symbol, columns=date)}"""
    defaults = {
        "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
        "volume": 100_000, "amount": 1_020_000.0,
        "turnoverRatio": 0.003, "suspendFlag": 0,
    }
    out = {}
    for f in fields:
        df = pd.DataFrame(index=symbols, columns=[date], dtype="float64")
        for s in symbols:
            df.loc[s, date] = defaults.get(f, 0)
        out[f] = df
    return out


@pytest.fixture
def fake_xtdata(monkeypatch):
    fake = SimpleNamespace(
        data_dir="/tmp/fake",
        get_trading_dates=MagicMock(return_value=["20260421", "20260422"]),
        get_stock_list_in_sector=MagicMock(return_value=["600519.SH", "000001.SZ"]),
        download_history_data=MagicMock(return_value=None),
        get_market_data=MagicMock(),
    )
    fake.get_market_data.side_effect = (
        lambda fields, syms, *_a, **_kw: _fake_market_data_frame(syms, "20260422", fields)
    )
    monkeypatch.setitem(sys.modules, "xtquant", SimpleNamespace(xtdata=fake))
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake)
    return fake


def test_download_happy_path(fake_xtdata):
    from src.market_data_download.downloader import download_daily_market_data

    result = download_daily_market_data(
        trade_date="20260422",
        sector_name="沪深A股",
    )

    assert result["trade_date"] == "20260422"
    assert set(result["symbols"]) == {"600519.SH", "000001.SZ"}
    fake_xtdata.download_history_data.assert_called()
    fake_xtdata.get_stock_list_in_sector.assert_called_with("沪深A股")
    for required in ("open", "high", "low", "close",
                     "volume", "amount", "turnoverRatio", "suspendFlag"):
        assert required in result["market_data"]


def test_download_non_trading_day_raises(fake_xtdata):
    from src.market_data_download.downloader import download_daily_market_data

    fake_xtdata.get_trading_dates.return_value = ["20260421", "20260422"]

    with pytest.raises(ValueError, match="非交易日"):
        download_daily_market_data(trade_date="20260425", sector_name="沪深A股")


def test_download_sleeps_before_reading(fake_xtdata, monkeypatch):
    """v3 历史教训：download 后必须 time.sleep(>=1) 再 get_market_data"""
    from src.market_data_download import downloader as dl_mod

    sleep_calls: list[float] = []
    monkeypatch.setattr(dl_mod.time, "sleep", lambda s: sleep_calls.append(s))

    dl_mod.download_daily_market_data(trade_date="20260422", sector_name="沪深A股")

    assert any(s >= 1 for s in sleep_calls), f"期望至少一次 sleep(>=1)，实际: {sleep_calls}"


def test_download_empty_sector_raises(fake_xtdata):
    from src.market_data_download.downloader import download_daily_market_data

    fake_xtdata.get_stock_list_in_sector.return_value = []

    with pytest.raises(RuntimeError, match="板块"):
        download_daily_market_data(trade_date="20260422", sector_name="不存在的板块")
