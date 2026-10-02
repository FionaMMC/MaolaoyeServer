from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from app.db import init_db, make_engine, make_session_factory
from app.models import (
    Order, RawSignal, ShadowFill, ShadowInstanceState, ShadowNavSnapshot, ShadowTarget, Trade,
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


def test_new_target_cannot_trade_at_previous_close(tmp_path):
    target = tmp_path / "shadow.parquet"
    target_frame().to_parquet(target, index=False)
    service, sf, store = make_service(tmp_path, config_for(target))
    add_prices(store, 20260630)
    result = service.run_all(20260701)["instances"][0]
    assert result["status"] == "blocked"
    with sf() as session:
        state = session.get(ShadowInstanceState, "Shadow_Base")
        assert state.virtual_cash == 1_000_000
        assert state.virtual_positions == {}
        assert session.query(ShadowTarget).count() == 0


def test_active_book_cannot_rewind_into_history(tmp_path):
    target = tmp_path / "shadow.parquet"
    target_frame().to_parquet(target, index=False)
    service, sf, store = make_service(tmp_path, config_for(target))
    add_prices(store, 20260701)
    add_prices(store, 20260702, stock=11)
    service.run_all(20260702)
    result = service.run_all(20260701)["instances"][0]
    assert result["status"] == "blocked"
    with sf() as session:
        assert session.get(ShadowNavSnapshot, ("Shadow_Base", "20260701")) is None


def test_close_fills_are_auditable_idempotent_and_sell_funds_buy(tmp_path):
    target = tmp_path / "shadow.parquet"
    frame = target_frame()
    frame["weight"] = [0.9, 0.1]
    frame.to_parquet(target, index=False)
    service, sf, store = make_service(tmp_path, config_for(target))
    for date in (20260701, 20260702):
        for category, code, opening, closing in (
            ("stocks", "000001.SZ", 7.0, 10.0),
            ("etfs", "511260.SH", 80.0, 100.0),
        ):
            store.append(category, code, pd.DataFrame({
                "trade_date": [date], "open": [opening], "close": [closing],
                "volume": [1_000_000], "suspendFlag": [0],
            }))
    service.run_all(20260701)
    with sf() as session:
        before = session.get(ShadowInstanceState, "Shadow_Base").virtual_cash
    frame["weight"] = [0.1, 0.9]
    frame["decision_date"] = "20260702"
    frame["input_hash"] = "b" * 64
    frame.to_parquet(target, index=False)
    result = service.run_all(20260702)["instances"][0]
    assert result["status"] == "active"
    service.run_all(20260702)
    with sf() as session:
        fills = session.query(ShadowFill).filter_by(trade_date="20260702").all()
        assert len(fills) == 2
        sell = next(f for f in fills if f.direction == "SELL")
        buy = next(f for f in fills if f.direction == "BUY")
        assert sell.price == 10.0 and buy.price == 100.0
        assert sell.price_basis == buy.price_basis == "OFFICIAL_DAILY_CLOSE"
        assert buy.quantity * buy.price + buy.fee > before
        assert sell.cash_after == pytest.approx(before + sell.quantity * sell.price - sell.fee)
        assert buy.cash_after == pytest.approx(sell.cash_after - buy.quantity * buy.price - buy.fee)
        state = session.get(ShadowInstanceState, "Shadow_Base")
        assert state.virtual_cash == pytest.approx(buy.cash_after)
        assert state.virtual_cash >= 0
        assert result["transaction_cost"] == pytest.approx(sum(f.fee for f in fills))
        assert session.query(Order).count() == session.query(Trade).count() == 0


@pytest.mark.parametrize("volume,suspended", [(0, 0), (1000, 1)])
def test_nontrading_close_cannot_create_shadow_fills(tmp_path, volume, suspended):
    target = tmp_path / "shadow.parquet"
    target_frame().to_parquet(target, index=False)
    service, sf, store = make_service(tmp_path, config_for(target))
    store.append("stocks", "000001.SZ", pd.DataFrame({
        "trade_date": [20260701], "close": [10],
        "volume": [volume], "suspendFlag": [suspended],
    }))
    store.append("etfs", "511260.SH", pd.DataFrame({
        "trade_date": [20260701], "close": [100], "volume": [1000],
    }))
    assert service.run_all(20260701)["instances"][0]["status"] == "blocked"
    with sf() as session:
        assert session.query(ShadowFill).count() == 0
        assert session.get(ShadowInstanceState, "Shadow_Base").virtual_positions == {}


def test_unchanged_holdings_can_still_use_stale_close_for_valuation(tmp_path):
    target = tmp_path / "shadow.parquet"
    target_frame().to_parquet(target, index=False)
    service, sf, store = make_service(tmp_path, config_for(target))
    add_prices(store, 20260701)
    first = service.run_all(20260701)["instances"][0]
    later = service.run_all(20260702)["instances"][0]
    assert later["nav"] == first["nav"]
    assert later["transaction_cost"] == 0
    with sf() as session:
        assert session.query(ShadowFill).count() == 2


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


def test_expired_target_keeps_valuing_accepted_book_without_trading(tmp_path):
    target = tmp_path / "shadow.parquet"
    target_frame().to_parquet(target, index=False)
    service, sf, store = make_service(tmp_path, config_for(target))
    add_prices(store, 20260701)
    service.run_all(20260701)
    with sf() as session:
        state = session.get(ShadowInstanceState, "Shadow_Base")
        frozen = (state.virtual_cash, dict(state.virtual_positions), state.target_hash, state.cumulative_cost)
    # A stale file with changed weights must not replace the accepted book.
    stale = target_frame()
    stale["weight"] = [0.1, 0.9]
    stale.to_parquet(target, index=False)
    add_prices(store, 20260903, stock=12)
    result = service.run_all(20260903)["instances"][0]
    assert result["status"] == "stale_target" and result["valuation_only"]
    assert result["transaction_cost"] == result["turnover"] == 0
    with sf() as session:
        state = session.get(ShadowInstanceState, "Shadow_Base")
        assert frozen == (state.virtual_cash, state.virtual_positions, state.target_hash, state.cumulative_cost)
        snapshot = session.get(ShadowNavSnapshot, ("Shadow_Base", "20260903"))
        assert snapshot and "valuation_only" in snapshot.state_reason
        assert session.query(Order).count() == session.query(Trade).count() == 0
    assert service.run_all(20260920)["instances"][0]["status"] == "blocked"  # stale prices
    assert service.run_all(20260902)["instances"][0]["status"] == "blocked"  # backward book


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
    assert cash_aux["enabled"] is False
    assert short_credit_aux["enabled"] is False
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


def test_symbol_allowlist_exemption_requires_exact_approved_fallback_reason(tmp_path):
    config = tmp_path / "strategies.yaml"
    config.write_text("""
shadow_instances:
  - shadow_id: Shadow_Aux_Hard_TOP2
    mode: shadow
    orders_enabled: false
    target_file: target.parquet
    allowed_symbols: [511880.SH]
    allowed_symbol_fallback_state_reasons:
      - BASE:TOP50:top2_not_applicable
      - BASE:T1_5050:top2_not_applicable
""", encoding="utf-8")
    service = ShadowLedgerService(None, None, config)
    constraints = service.load_instances()[0]
    frame = pd.DataFrame([{
        "shadow_id": "Shadow_Aux_Hard_TOP2",
        "code": "000001.SZ",
        "weight": 1.0,
        "decision_date": "20260807",
        "as_of_date": "20260731",
        "state_reason": "BASE:T1_5050:top2_not_applicable",
        "source_version": "v7.9-hard-logistic-aux-top2-r1@approved",
        "input_hash": "a" * 64,
    }])

    service.validate_target(
        frame, "Shadow_Aux_Hard_TOP2", 20260807, constraints=constraints
    )

    active_aux = frame.copy()
    active_aux["state_reason"] = "AUX_HYDRA:rotation_top2"
    with pytest.raises(ValueError, match="allowlist"):
        service.validate_target(
            active_aux, "Shadow_Aux_Hard_TOP2", 20260807,
            constraints=constraints,
        )

    almost_fallback = frame.copy()
    almost_fallback["state_reason"] = "BASE:T1_5050:top2_not_applicable:extra"
    with pytest.raises(ValueError, match="allowlist"):
        service.validate_target(
            almost_fallback, "Shadow_Aux_Hard_TOP2", 20260807,
            constraints=constraints,
        )


def test_metadata_republication_does_not_reset_drifted_holdings(tmp_path):
    target = tmp_path / "target.parquet"
    frame = target_frame()
    frame.to_parquet(target,index=False)
    service,sf,store = make_service(tmp_path,config_for(target))
    add_prices(store,20260701)
    service.run_all(20260701)
    with sf() as session:
        old = session.get(ShadowInstanceState,"Shadow_Base")
        positions,cash,cost = dict(old.virtual_positions),old.virtual_cash,old.cumulative_cost
    # Material market drift would cause needless turnover under artifact hashing.
    add_prices(store,20260708,stock=12.0,etf=98.0)
    frame["decision_date"] = "20260708"
    frame["input_hash"] = "b"*64
    frame.to_parquet(target,index=False)
    result = service.run_all(20260708)["instances"][0]
    assert result["transaction_cost"] == result["turnover"] == 0
    with sf() as session:
        new = session.get(ShadowInstanceState,"Shadow_Base")
        assert (new.virtual_positions,new.virtual_cash,new.cumulative_cost) == (positions,cash,cost)
        assert new.decision_date == "20260708"
        assert session.query(ShadowTarget).count() == 4  # new provenance is retained
    # New monthly intent still rebalances the same weights.
    frame["decision_date"] = "20260803"
    frame["as_of_date"] = "20260731"
    frame.to_parquet(target,index=False)
    add_prices(store,20260803,stock=12.0,etf=98.0)
    assert service.run_all(20260803)["instances"][0]["transaction_cost"] > 0


def test_monthly_control_holds_revised_weights_but_keeps_valuing(tmp_path):
    target = tmp_path / "target.parquet"
    frame = target_frame()
    frame.to_parquet(target, index=False)
    service, sf, store = make_service(
        tmp_path, config_for(target) + "    rebalance_cadence: monthly\n"
    )
    add_prices(store, 20260701)
    first = service.run_all(20260701)["instances"][0]
    with sf() as session:
        old = session.get(ShadowInstanceState, "Shadow_Base")
        positions, cash, cost = dict(old.virtual_positions), old.virtual_cash, old.cumulative_cost
    frame["decision_date"] = "20260708"
    frame["weight"] = [0.2, 0.8]
    frame.to_parquet(target, index=False)
    add_prices(store, 20260708, stock=12.0, etf=98.0)
    result = service.run_all(20260708)["instances"][0]
    assert result["held_reason"] == "monthly_cycle_already_consumed"
    assert result["turnover"] == result["transaction_cost"] == 0
    assert result["nav"] != first["nav"]
    with sf() as session:
        new = session.get(ShadowInstanceState, "Shadow_Base")
        assert (new.virtual_positions, new.virtual_cash, new.cumulative_cost) == (positions, cash, cost)
        assert new.target_hash == first["target_hash"]
        assert session.query(ShadowTarget).count() == 2
        assert session.get(ShadowNavSnapshot, ("Shadow_Base", "20260708")) is not None
        assert session.query(Order).count() == session.query(Trade).count() == 0
    # The next cycle can trade the revised weights, then refuses rollback.
    frame["decision_date"] = "20260803"
    frame["as_of_date"] = "20260731"
    frame.to_parquet(target, index=False)
    add_prices(store, 20260803, stock=12.0, etf=98.0)
    second = service.run_all(20260803)["instances"][0]
    assert second["transaction_cost"] > 0
    assert second["held_reason"] is None
    frame["decision_date"] = "20260804"
    frame["as_of_date"] = "20260630"
    frame.to_parquet(target, index=False)
    add_prices(store, 20260804, stock=12.2, etf=98.0)
    rollback = service.run_all(20260804)["instances"][0]
    assert rollback["held_reason"] == "monthly_cycle_rollback_rejected"
    assert rollback["turnover"] == 0
    assert rollback["target_hash"] == second["target_hash"]


def test_monthly_cadence_rejects_unknown_configuration(tmp_path):
    service, _, _ = make_service(
        tmp_path, config_for(tmp_path / "target.parquet") + "    rebalance_cadence: weekly_typo\n"
    )
    with pytest.raises(ShadowBoundaryError, match="rebalance_cadence"):
        service.load_instances()
