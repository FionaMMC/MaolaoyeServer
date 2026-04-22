from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.market_data_download.storage import save_market_data_parquet


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": ["600519.SH"],
        "trade_date": ["20260422"],
        "open": [1520.0],
        "high": [1548.0],
        "low": [1515.0],
        "close": [1540.0],
        "volume": [12345678],
        "amount": [19012345678.0],
        "turnover_rate": [0.0032],
        "is_suspended": [False],
    })


def test_save_creates_file(tmp_path: Path):
    df = _sample_df()

    out_path = save_market_data_parquet(df, trade_date="20260422", data_root=tmp_path)

    assert out_path == tmp_path / "market_data" / "20260422.parquet"
    assert out_path.exists()

    loaded = pd.read_parquet(out_path)
    pd.testing.assert_frame_equal(loaded, df)


def test_save_overwrites_existing(tmp_path: Path):
    df1 = _sample_df()
    df2 = _sample_df().assign(close=[9999.0])

    save_market_data_parquet(df1, trade_date="20260422", data_root=tmp_path)
    out2 = save_market_data_parquet(df2, trade_date="20260422", data_root=tmp_path)

    loaded = pd.read_parquet(out2)
    assert loaded["close"].iloc[0] == 9999.0
