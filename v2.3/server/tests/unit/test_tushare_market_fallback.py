from pathlib import Path

import pandas as pd
import pytest

from scripts.fetch_market_from_tushare import run_fallback


def _seed(path: Path, code: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{
        "trade_date": 20260727,
        "open": 10.0,
        "high": 10.0,
        "low": 10.0,
        "close": 10.0,
        "volume": 100,
        "amount": 100_000.0,
        "suspendFlag": 0,
    }]).to_parquet(path / f"{code}.parquet", index=False)


def _store(tmp_path: Path) -> Path:
    store = tmp_path / "daily"
    _seed(store / "stocks", "600000.SH")
    _seed(store / "etfs", "159915.SZ")
    _seed(store / "indexes", "000852.SH")
    return store


def _fetch(api_name: str, trade_date: str, token: str) -> pd.DataFrame:
    codes = {"daily": "600000.SH", "fund_daily": "159915.SZ", "index_daily": "000852.SH"}
    return pd.DataFrame([{
        "ts_code": codes[api_name],
        "trade_date": trade_date,
        "open": 10.0014,
        "high": 10.2015,
        "low": 9.9014,
        "close": 10.1015,
        "vol": 123.6,
        "amount": 456.7894,
    }])


def test_fallback_appends_normalized_bars_atomically(tmp_path):
    store = _store(tmp_path)

    summary = run_fallback(store, "20260728", "token", fetcher=_fetch)

    assert summary["source"] == "tushare_eod_fallback"
    for category, code in (("stocks", "600000.SH"), ("etfs", "159915.SZ"),
                           ("indexes", "000852.SH")):
        data = pd.read_parquet(store / category / f"{code}.parquet")
        row = data[data.trade_date == 20260728].iloc[0]
        assert row["close"] == 10.102
        assert row["volume"] == 124
        assert row["amount"] == 456789.0
    assert (store.parent / "fallback_audit" / "market_20260728.json").exists()


def test_fallback_dry_run_does_not_write(tmp_path):
    store = _store(tmp_path)

    summary = run_fallback(store, "20260728", "token", dry_run=True, fetcher=_fetch)

    assert summary["dry_run"] is True
    assert pd.read_parquet(store / "indexes" / "000852.SH.parquet").trade_date.max() == 20260727


def test_fallback_rejects_low_coverage_before_any_write(tmp_path):
    store = _store(tmp_path)

    def empty_stock(api_name: str, trade_date: str, token: str) -> pd.DataFrame:
        if api_name == "daily":
            return pd.DataFrame(columns=[
                "ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount",
            ])
        return _fetch(api_name, trade_date, token)

    with pytest.raises(RuntimeError, match="stocks coverage"):
        run_fallback(store, "20260728", "token", fetcher=empty_stock)

    assert pd.read_parquet(store / "indexes" / "000852.SH.parquet").trade_date.max() == 20260727


def test_fallback_skips_when_qmt_probe_is_already_fresh(tmp_path):
    store = _store(tmp_path)
    index_path = store / "indexes" / "000852.SH.parquet"
    current = pd.read_parquet(index_path)
    current = pd.concat([current, current.assign(trade_date=20260728)], ignore_index=True)
    current.to_parquet(index_path, index=False)

    summary = run_fallback(
        store, "20260728", "token",
        fetcher=lambda *_: pytest.fail("fresh store must not call Tushare"),
    )

    assert summary["skipped"] == "qmt_store_already_fresh"
