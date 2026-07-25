from __future__ import annotations

import json
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


def config_for_required_sidecar(target: Path) -> str:
    return f"""
shadow_instances:
  - shadow_id: Shadow_Base
    mode: shadow
    orders_enabled: false
    initial_cash: 1000000
    target_file: {target}
    require_sidecar: true
"""


def write_sidecar(target: Path, frame: pd.DataFrame, **overrides) -> None:
    sidecar = {
        "shadow_id": frame["shadow_id"].iloc[0],
        "decision_date": frame["decision_date"].iloc[0],
        "as_of_date": frame["as_of_date"].iloc[0],
        "source_version": frame["source_version"].iloc[0],
        "input_hash": frame["input_hash"].iloc[0],
        "weight_sum": float(frame["weight"].sum()),
    }
    sidecar.update(overrides)
    target.with_suffix(".json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )


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


def test_direct_ledger_requires_and_validates_producer_sidecar(tmp_path):
    target = tmp_path / "shadow.parquet"
    frame = target_frame()
    frame.to_parquet(target, index=False)
    service, sf, store = make_service(
        tmp_path, config_for_required_sidecar(target)
    )
    add_prices(store, 20260701)

    missing = service.run_all(20260701)["instances"][0]
    assert missing["status"] == "blocked"
    assert "sidecar missing" in missing["reason"]

    write_sidecar(target, frame, input_hash="b" * 64)
    tampered = service.run_all(20260701)["instances"][0]
    assert tampered["status"] == "blocked"
    assert "input_hash does not match" in tampered["reason"]

    write_sidecar(target, frame)
    valid = service.run_all(20260701)["instances"][0]
    assert valid["status"] == "active"
    with sf() as session:
        assert session.query(ShadowTarget).count() == 2
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


def test_hydra_shadow_config_pins_source_symbols_and_target_age():
    config = Path(__file__).resolve().parents[2] / "strategies.yaml"
    service = ShadowLedgerService(None, None, config)
    instances = {
        item["shadow_id"]: item for item in service.load_instances()
    }
    hydra = instances["Shadow_Hydra_V481_RB"]

    assert {
        shadow_id for shadow_id, item in instances.items()
        if item["require_sidecar"]
    } == {
        "Shadow_Base", "Shadow_Aux_Hard_TOP2", "Shadow_Aux_Hard_TOP2_ShortCredit", "Shadow_ML_TOP2",
        "Shadow_Hydra_V481_RB",
    }
    assert {
        instances[shadow_id]["max_target_age_days"]
        for shadow_id in (
            "Shadow_Base", "Shadow_Aux_Hard_TOP2", "Shadow_Aux_Hard_TOP2_ShortCredit", "Shadow_ML_TOP2"
        )
    } == {40}
    cash_aux = instances["Shadow_Aux_Hard_TOP2"]
    short_credit_aux = instances["Shadow_Aux_Hard_TOP2_ShortCredit"]
    assert cash_aux["enabled"] is True
    assert short_credit_aux["enabled"] is True
    assert cash_aux["allowed_source_versions"] == [
        "v7.9-hard-logistic-aux-top2-r1@88c2cb1050c7391ce84a9d524a9884dfefaf3ef4"
    ]
    assert short_credit_aux["allowed_source_versions"] == [
        "v7.9-hard-logistic-aux-top2-short-credit-r1@88c2cb1050c7391ce84a9d524a9884dfefaf3ef4"
    ]
    assert "511880.SH" in cash_aux["allowed_symbols"]
    assert "511360.SH" in short_credit_aux["allowed_symbols"]
    assert hydra["require_sidecar"] is True
    assert hydra["allowed_source_versions"] == [
        "v48.1-RB@49c16dadc298d6a51470bd5c2f931ecc36f65460"
    ]
    assert hydra["allowed_publisher_source_commits"] == [
        "66985a19621e9dc8b5f2525e57ba1696fa7a9236"
    ]
    assert set(hydra["allowed_symbols"]) == {
        "510300.SH", "159915.SZ", "511260.SH", "518880.SH", "159981.SZ",
        "159985.SZ", "159930.SZ", "513500.SH", "513100.SH",
    }
    assert set(hydra["required_symbols"]) == set(hydra["allowed_symbols"])
    assert hydra["max_target_age_days"] == 40
    assert hydra["commission_rate"] == pytest.approx(0.0001)
    assert hydra["stamp_duty_sell"] == 0.0
    assert hydra["target_file"].name == "Shadow_Hydra_V481_RB_latest.parquet"


def test_hydra_shadow_rejects_unapproved_source_symbol_and_stale_target(tmp_path):
    config = tmp_path / "strategies.yaml"
    config.write_text("""
shadow_instances:
  - shadow_id: Shadow_Hydra_V481_RB
    mode: shadow
    orders_enabled: false
    target_file: target.parquet
    max_target_age_days: 40
    allowed_symbols: [511260.SH]
    allowed_source_versions:
      - v48.1-RB@approved
""", encoding="utf-8")
    service = ShadowLedgerService(None, None, config)
    constraints = service.load_instances()[0]
    frame = pd.DataFrame([{
        "shadow_id": "Shadow_Hydra_V481_RB",
        "code": "511260.SH",
        "weight": 1.0,
        "decision_date": "20260717",
        "as_of_date": "20260717",
        "state_reason": "DYNAMIC_BOND_RISK_BUDGET",
        "source_version": "v48.1-RB@approved",
        "input_hash": "a" * 64,
    }])

    service.validate_target(
        frame, "Shadow_Hydra_V481_RB", 20260725, constraints=constraints
    )

    bad_source = frame.copy()
    bad_source["source_version"] = "v48.1-RB@unreviewed"
    with pytest.raises(ValueError, match="source_version"):
        service.validate_target(
            bad_source, "Shadow_Hydra_V481_RB", 20260725,
            constraints=constraints,
        )

    bad_symbol = frame.copy()
    bad_symbol["code"] = "512890.SH"
    with pytest.raises(ValueError, match="allowlist"):
        service.validate_target(
            bad_symbol, "Shadow_Hydra_V481_RB", 20260725,
            constraints=constraints,
        )

    required_constraints = {**constraints, "required_symbols": ["511260.SH", "518880.SH"]}
    with pytest.raises(ValueError, match="missing required"):
        service.validate_target(
            frame, "Shadow_Hydra_V481_RB", 20260725,
            constraints=required_constraints,
        )

    with pytest.raises(ValueError, match="stale"):
        service.validate_target(
            frame, "Shadow_Hydra_V481_RB", 20260901,
            constraints=constraints,
        )

    stale_as_of = frame.copy()
    stale_as_of["decision_date"] = "20260725"
    stale_as_of["as_of_date"] = "20260601"
    with pytest.raises(ValueError, match="as_of_date=20260601"):
        service.validate_target(
            stale_as_of, "Shadow_Hydra_V481_RB", 20260725,
            constraints=constraints,
        )
