from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from app.db import init_db, make_engine, make_session_factory
from app.models import (
    Order, RawSignal, ShadowInstanceState, ShadowNavSnapshot, ShadowTarget, Trade,
)
from app.services.shadow_ledger import ShadowBoundaryError, ShadowLedgerService
from app.storage.parquet import ParquetStore


def make_service(tmp_path: Path, config_text: str):
    config = tmp_path / "strategies.yaml"
    config.write_text(config_text, encoding="utf-8")
    engine = make_engine(f"sqlite:///{tmp_path}/shadow.db")
    init_db(engine)
    sf = make_session_factory(engine)
    store = ParquetStore(tmp_path / "market")
    return ShadowLedgerService(sf, store, config), sf, store


def config_for(target: Path) -> str:
    return f"""
shadow_instances:
  - shadow_id: Shadow_Base
    mode: shadow
    orders_enabled: false
    initial_cash: 1000000
    target_file: {target}
"""


def target_frame(input_hash: str = "a" * 64) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "shadow_id": "Shadow_Base", "code": "000001.SZ", "weight": 0.6,
            "decision_date": "20260701", "as_of_date": "20260630",
            "state_reason": "frozen base", "source_version": "v7.13-base@2538554",
            "input_hash": input_hash,
        },
        {
            "shadow_id": "Shadow_Base", "code": "511260.SH", "weight": 0.4,
            "decision_date": "20260701", "as_of_date": "20260630",
            "state_reason": "frozen base", "source_version": "v7.13-base@2538554",
            "input_hash": input_hash,
        },
    ])


def add_prices(store: ParquetStore, trade_date: int, stock=10.0, etf=100.0):
    store.append("stocks", "000001.SZ", pd.DataFrame({
        "trade_date": [trade_date], "close": [stock], "volume": [1_000_000],
    }))
    store.append("etfs", "511260.SH", pd.DataFrame({
        "trade_date": [trade_date], "close": [etf], "volume": [1_000_000],
    }))


def test_shadow_rebalance_and_daily_nav_never_touch_order_tables(tmp_path):
    target = tmp_path / "shadow.parquet"
    target_frame().to_parquet(target, index=False)
    service, sf, store = make_service(tmp_path, config_for(target))
    add_prices(store, 20260701)

    first = service.run_all(20260701)["instances"][0]
    assert first["status"] == "active"
    assert first["transaction_cost"] > 0
    service.run_all(20260701)
    with sf() as session:
        same_day = session.get(ShadowNavSnapshot, ("Shadow_Base", "20260701"))
        assert same_day.transaction_cost == first["transaction_cost"]
        assert same_day.turnover == first["turnover"]

    # Same content is mark-to-market only, not a second rebalance.
    add_prices(store, 20260702, stock=10.1, etf=100.2)
    second = service.run_all(20260702)["instances"][0]
    assert second["transaction_cost"] == 0
    assert second["turnover"] == 0
    assert second["nav"] != first["nav"]

    with sf() as session:
        assert session.query(RawSignal).count() == 0
        assert session.query(Order).count() == 0
        assert session.query(Trade).count() == 0
        assert session.query(ShadowTarget).count() == 2
        assert session.query(ShadowNavSnapshot).count() == 2
        state = session.get(ShadowInstanceState, "Shadow_Base")
        assert state.status == "active"
        assert state.virtual_positions


def test_shadow_target_schema_and_hash_fail_closed(tmp_path):
    target = tmp_path / "shadow.parquet"
    bad = target_frame(input_hash="not-a-hash")
    bad.to_parquet(target, index=False)
    service, sf, store = make_service(tmp_path, config_for(target))
    add_prices(store, 20260701)

    result = service.run_all(20260701)["instances"][0]
    assert result["status"] == "blocked"
    assert "SHA-256" in result["reason"]
    with sf() as session:
        assert session.query(ShadowTarget).count() == 0
        assert session.query(Order).count() == 0


def test_shadow_configuration_rejects_account_or_order_permission(tmp_path):
    service, _, _ = make_service(tmp_path, """
shadow_instances:
  - shadow_id: Shadow_ML_TOP2
    mode: shadow
    qmt_account_id: forbidden
    orders_enabled: false
    target_file: missing.parquet
""")
    with pytest.raises(ShadowBoundaryError, match="must not bind"):
        service.load_instances()

    service.config_path.write_text("""
shadow_instances:
  - shadow_id: Shadow_ML_TOP2
    mode: shadow
    orders_enabled: true
    target_file: missing.parquet
""", encoding="utf-8")
    with pytest.raises(ShadowBoundaryError, match="must not enable orders"):
        service.load_instances()


def test_future_target_and_stale_price_are_blocked(tmp_path):
    target = tmp_path / "shadow.parquet"
    frame = target_frame()
    frame["decision_date"] = "20260703"
    frame.to_parquet(target, index=False)
    service, _, store = make_service(tmp_path, config_for(target))
    add_prices(store, 20260701)
    result = service.run_all(20260701)["instances"][0]
    assert result["status"] == "blocked"
    assert "future" in result["reason"]
