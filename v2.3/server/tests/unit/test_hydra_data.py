"""Hydra 双数据链的口径、hash、schema 与不可变性。"""
from __future__ import annotations

import hashlib
import io

import pandas as pd
import pytest

from app.schemas.hydra_data import HydraDataManifest
from app.services.hydra_data import HydraDataStore


def _parquet_bytes(frame: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    frame.to_parquet(output, index=False)
    return output.getvalue()


def _prices() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "symbol": "510300.SH", "trade_date": "20260731",
            "open": 4.0, "high": 4.1, "low": 3.9, "close": 4.05,
            "volume": 1000, "amount": 4050.0, "suspendFlag": 0,
        },
        {
            "symbol": "159915.SZ", "trade_date": "20260731",
            "open": 2.0, "high": 2.1, "low": 1.9, "close": 2.05,
            "volume": 1000, "amount": 2050.0, "suspendFlag": 0,
        },
    ])


def _manifest(body: bytes, **changes) -> HydraDataManifest:
    payload = {
        "stream": "hydra_execution_raw",
        "source": "mock_qmt",
        "adjustment": "none",
        "as_of_date": "20260731",
        "fetched_at": "2026-07-31T16:10:00+08:00",
        "producer_commit": "1" * 40,
        "file_sha256": hashlib.sha256(body).hexdigest(),
        "row_count": 2,
        "symbol_count": 2,
    }
    payload.update(changes)
    return HydraDataManifest(**payload)


def test_install_and_load_content_addressed_batch(tmp_path):
    body = _parquet_bytes(_prices())
    store = HydraDataStore(tmp_path)
    first = store.install(body, _manifest(body))
    second = store.install(body, _manifest(body))
    frame, manifest = store.load("hydra_execution_raw", first.file_sha256)
    assert first.installed is True
    assert second.installed is False
    assert frame["symbol"].tolist() == ["510300.SH", "159915.SZ"]
    assert manifest.adjustment == "none"


def test_execution_raw_rejects_back_adjustment(tmp_path):
    body = _parquet_bytes(_prices())
    with pytest.raises(ValueError, match="adjustment 必须是 none"):
        HydraDataStore(tmp_path).install(body, _manifest(body, adjustment="back"))


def test_manifest_rejects_wrong_file_hash(tmp_path):
    body = _parquet_bytes(_prices())
    with pytest.raises(ValueError, match="file_sha256"):
        HydraDataStore(tmp_path).install(body, _manifest(body, file_sha256="0" * 64))


@pytest.mark.parametrize("column,value", [
    ("close", float("nan")),
    ("open", 0.0),
    ("suspendFlag", 2),
])
def test_price_batch_rejects_dirty_execution_values(tmp_path, column, value):
    frame = _prices()
    frame.loc[0, column] = value
    body = _parquet_bytes(frame)
    with pytest.raises(ValueError):
        HydraDataStore(tmp_path).install(body, _manifest(body))


def test_price_batch_rejects_as_of_mismatch(tmp_path):
    body = _parquet_bytes(_prices())
    with pytest.raises(ValueError, match="as_of_date"):
        HydraDataStore(tmp_path).install(body, _manifest(body, as_of_date="20260730"))


@pytest.mark.parametrize(("column", "value"), [
    ("volume", -1),
    ("amount", float("inf")),
    ("high", 3.8),
    ("low", 4.2),
])
def test_price_batch_rejects_negative_flow_or_invalid_ohlc(
    tmp_path, column, value,
):
    frame = _prices()
    frame.loc[0, column] = value
    body = _parquet_bytes(frame)
    with pytest.raises(ValueError):
        HydraDataStore(tmp_path).install(body, _manifest(body))


def test_suspended_row_allows_zero_intraday_prices_but_requires_close(tmp_path):
    frame = _prices()
    frame.loc[0, ["open", "high", "low", "volume", "amount"]] = 0
    frame.loc[0, "suspendFlag"] = 1
    body = _parquet_bytes(frame)
    installed = HydraDataStore(tmp_path).install(body, _manifest(body))
    assert installed.installed is True


def test_empty_corporate_action_stream_is_valid_audit_evidence(tmp_path):
    frame = pd.DataFrame(columns=[
        "symbol", "event_date", "event_type", "cash_per_share",
        "share_factor", "source_event_id",
    ])
    body = _parquet_bytes(frame)
    manifest = _manifest(
        body,
        stream="hydra_corporate_actions",
        adjustment="corporate_actions",
        row_count=0,
        symbol_count=0,
    )
    installed = HydraDataStore(tmp_path).install(body, manifest)
    assert installed.row_count == 0


def test_trading_calendar_is_content_addressed_and_contains_as_of(tmp_path):
    frame = pd.DataFrame({"trade_date": ["20260731", "20260803"]})
    body = _parquet_bytes(frame)
    manifest = HydraDataManifest(
        stream="hydra_trading_calendar",
        source="mock_qmt",
        adjustment="calendar",
        as_of_date="20260731",
        fetched_at="2026-07-31T16:10:00+08:00",
        producer_commit="1" * 40,
        file_sha256=hashlib.sha256(body).hexdigest(),
        row_count=2,
        symbol_count=0,
    )
    installed = HydraDataStore(tmp_path).install(body, manifest)
    loaded, _ = HydraDataStore(tmp_path).load(
        "hydra_trading_calendar", installed.file_sha256,
    )
    assert loaded["trade_date"].astype(str).tolist() == ["20260731", "20260803"]
