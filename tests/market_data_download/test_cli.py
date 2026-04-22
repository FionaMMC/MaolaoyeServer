from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest


def _write_cfg(path: Path, data_root: str) -> Path:
    cfg = path / "settings.yaml"
    cfg.write_text(
        f"""
qmt:
  data_dir: "/tmp/fake_qmt"
  account_id: "ACC"
server:
  base_url: "https://x"
  api_key: "K"
  timeout: 10
paths:
  data_root: "{data_root}"
  log_dir: "{data_root}/logs"
  sqlite_path: "{data_root}/trading.db"
notify:
  wecom_webhook: "w"
market_data:
  sector_name: "沪深A股"
""",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def fake_xtdata(monkeypatch):
    fake = SimpleNamespace(
        data_dir="",
        get_trading_dates=MagicMock(return_value=["20260422"]),
        get_stock_list_in_sector=MagicMock(return_value=["600519.SH"]),
        download_history_data=MagicMock(return_value=None),
        get_market_data=MagicMock(),
    )

    def _gmd(fields, syms, *_a, **_kw):
        defaults = {
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 1000, "amount": 10000.0,
            "turnoverRatio": 0.01, "suspendFlag": 0,
        }
        out = {}
        for f in fields:
            df = pd.DataFrame(index=syms, columns=["20260422"], dtype="float64")
            for s in syms:
                df.loc[s, "20260422"] = defaults[f]
            out[f] = df
        return out

    fake.get_market_data.side_effect = _gmd
    monkeypatch.setitem(sys.modules, "xtquant", SimpleNamespace(xtdata=fake))
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake)
    return fake


def test_cli_happy_path(fake_xtdata, tmp_path: Path):
    from src.market_data_download.__main__ import main

    data_root = tmp_path / "data"
    cfg = _write_cfg(tmp_path, str(data_root))

    exit_code = main(["--date", "20260422", "--config", str(cfg)])

    assert exit_code == 0
    assert (data_root / "market_data" / "20260422.parquet").exists()


def test_cli_missing_config_file(tmp_path: Path):
    from src.market_data_download.__main__ import main

    exit_code = main(["--date", "20260422", "--config", str(tmp_path / "nope.yaml")])

    assert exit_code == 1


def test_cli_non_trading_day(fake_xtdata, tmp_path: Path):
    from src.market_data_download.__main__ import main

    fake_xtdata.get_trading_dates.return_value = ["20260421"]
    data_root = tmp_path / "data"
    cfg = _write_cfg(tmp_path, str(data_root))

    exit_code = main(["--date", "20260425", "--config", str(cfg)])

    assert exit_code == 2


def test_cli_missing_date_arg(tmp_path: Path):
    from src.market_data_download.__main__ import main

    with pytest.raises(SystemExit):
        main(["--config", str(tmp_path / "x.yaml")])
