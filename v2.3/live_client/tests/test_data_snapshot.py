"""Research freezer stays account-free and emits an immutable aggregate receipt."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

from live_client import data_snapshot


def _prices(symbols: list[str], date: str) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "symbol": symbol, "trade_date": date, "open": 1.0, "high": 1.1,
            "low": 0.9, "close": 1.0, "volume": 100, "amount": 100.0,
            "suspendFlag": 0,
        }
        for symbol in symbols
    ])


def test_collect_prices_keeps_research_symbol_out_of_raw(monkeypatch, tmp_path: Path):
    calls: list[list[str]] = []

    class FakeXtdata:
        data_dir = ""

        @staticmethod
        def download_history_data2(symbols, *_args):
            calls.append(symbols)

        @staticmethod
        def get_market_data_ex(_fields, symbols, *_args, **_kwargs):
            return {
                symbol: pd.DataFrame(
                    {"open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0],
                     "volume": [100], "amount": [100.0], "suspendFlag": [0]},
                    index=pd.Index(["20260821"], name="trade_date"),
                )
                for symbol in symbols
            }

        @staticmethod
        def get_trading_calendar(*_args, **_kwargs):
            return ["20260821", "20260831"]

    monkeypatch.setitem(sys.modules, "xtquant", type("X", (), {"xtdata": FakeXtdata}))
    userdata = tmp_path / "userdata"
    userdata.mkdir()
    hfq, raw, _calendar = data_snapshot.collect_prices(
        data_snapshot.ResearchDataConfig(userdata), "20260821",
    )

    assert set(hfq.symbol) == data_snapshot.EXECUTABLE_SYMBOLS | data_snapshot.RESEARCH_ONLY_SYMBOLS
    assert set(raw.symbol) == data_snapshot.EXECUTABLE_SYMBOLS
    assert set(calls[0]) == data_snapshot.EXECUTABLE_SYMBOLS | data_snapshot.RESEARCH_ONLY_SYMBOLS


def test_main_writes_zip_and_receipt_without_live_config(monkeypatch, tmp_path: Path):
    as_of = "20260821"
    userdata = tmp_path / "userdata"
    userdata.mkdir()
    actions = tmp_path / "actions.parquet"
    pd.DataFrame([{
        "symbol": "510300.SH", "event_date": as_of, "event_type": "split",
        "cash_per_share": 0.0, "share_factor": 1.0, "source_event_id": "unit-test",
    }]).to_parquet(actions, index=False)
    monkeypatch.setattr(
        data_snapshot, "collect_prices",
        lambda *_args: (_prices(sorted(data_snapshot.EXECUTABLE_SYMBOLS | data_snapshot.RESEARCH_ONLY_SYMBOLS), as_of),
                         _prices(sorted(data_snapshot.EXECUTABLE_SYMBOLS), as_of), [as_of]),
    )
    output = tmp_path / "freeze"
    monkeypatch.setattr(sys, "argv", [
        "data_snapshot", "--as-of", as_of, "--producer-commit", "a" * 40,
        "--userdata-dir", str(userdata), "--corporate-actions", str(actions),
        "--output", str(output),
    ])

    data_snapshot.main()

    receipt = output.parent / "HYDRA_QMT_SNAPSHOT_20260821.manifest.json"
    body = json.loads(receipt.read_text(encoding="utf-8"))
    assert (output.parent / "HYDRA_QMT_SNAPSHOT_20260821.zip").is_file()
    assert len(body["zip_sha256"]) == 64
    assert body["snapshot_manifest"]["manifests"]["model_hfq"]["research_only_symbols"] == ["511010.SH"]


def test_freeze_rejects_one_stale_symbol_even_if_other_symbols_are_current(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    import pytest
    stale = sorted(data_snapshot.EXECUTABLE_SYMBOLS)[0]
    def payload(_fields, symbols, *_args, **_kwargs):
        return {s: pd.DataFrame({'open':[1.], 'high':[1.1], 'low':[.9], 'close':[1.],
                                'volume':[100], 'amount':[100.], 'suspendFlag':[0]},
                                index=['20260929' if s == stale else '20260930']) for s in symbols}
    xtdata=SimpleNamespace(download_history_data2=lambda *a:None,get_market_data_ex=payload)
    monkeypatch.setitem(sys.modules,'xtquant',SimpleNamespace(xtdata=xtdata))
    with pytest.raises(RuntimeError,match='未更新至 as_of_date'):
        data_snapshot.collect_prices(data_snapshot.ResearchDataConfig(tmp_path),'20260930')


def test_missing_corporate_action_response_is_not_proof_of_no_dividend(monkeypatch,tmp_path):
    import sys
    from types import SimpleNamespace
    import pytest
    xtdata=SimpleNamespace(get_divid_factors=lambda s:None)
    monkeypatch.setitem(sys.modules,'xtquant',SimpleNamespace(xtdata=xtdata))
    with pytest.raises(RuntimeError,match='查询无响应'):
        data_snapshot.collect_corporate_actions(data_snapshot.ResearchDataConfig(tmp_path),'20260930',require_response=True)
