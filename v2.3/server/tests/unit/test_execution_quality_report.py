"""Observed execution costs are descriptive, scoped, and money-weighted."""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import ExecutionQualityObservation
from app.services.execution_quality import summarize_execution_quality
from scripts.hydra_execution_quality_report import main


def observation(**overrides):
    fields = {
        "order_id": "one",
        "execution_domain": "live",
        "target_id": "target-a",
        "attempt_id": "attempt-a",
        "symbol": "510300.SH",
        "direction": "BUY",
        "decision_reference_price": 100.0,
        "arrival_reference_price": 100.0,
        "arrival_reference_time": "2026-09-08T09:25:00+08:00",
        "fill_vwap": 101.0,
        "filled_quantity": 100,
        "iopv": 100.0,
        "iopv_time": "2026-09-08T09:25:00+08:00",
        "estimated_fees": 5.0,
        "updated_at": "2026-09-08T15:10:00+08:00",
    }
    fields.update(overrides)
    return ExecutionQualityObservation(**fields)


def test_cross_price_etfs_use_benchmark_money_not_share_count():
    # Both legs have 10,000 benchmark notional; 100 bps and zero => 50 bps.
    result = summarize_execution_quality([
        observation(),
        observation(
            order_id="two", symbol="159915.SZ", filled_quantity=10_000,
            decision_reference_price=1, arrival_reference_price=1, fill_vwap=1, iopv=1,
        ),
    ])
    assert result["weighted_execution_shortfall_bps"] == pytest.approx(50)
    assert result["execution_shortfall"]["benchmark_notional"] == 20_000
    assert result["execution_shortfall"]["signed_price_difference_amount"] == 100
    assert result["schema_version"] == 2


@pytest.mark.parametrize(("side", "fill", "expected"), [
    ("BUY", 101, 100), ("SELL", 99, 100),
    ("BUY", 99, -100), ("SELL", 101, -100),
])
def test_positive_shortfall_is_adverse_for_either_side(side, fill, expected):
    result = summarize_execution_quality([observation(direction=side, fill_vwap=fill)])
    assert result["weighted_execution_shortfall_bps"] == pytest.approx(expected)


def test_decision_gap_shortfall_premium_and_fees_are_not_added_together():
    result = summarize_execution_quality([
        observation(arrival_reference_price=110, fill_vwap=111, estimated_fees=7),
    ])
    assert result["weighted_decision_gap_bps"] == pytest.approx(1000)
    assert result["weighted_execution_shortfall_bps"] == pytest.approx(10_000 / 110)
    assert result["weighted_premium_bps"] == pytest.approx(1000)
    assert result["estimated_fees"] == 7
    assert result["decision_gap"]["benchmark_notional"] == 10_000
    assert result["execution_shortfall"]["benchmark_notional"] == 11_000


def test_premium_is_not_directional_execution_cost():
    result = summarize_execution_quality([
        observation(direction="SELL", arrival_reference_price=110, fill_vwap=110),
    ])
    assert result["weighted_premium_bps"] == pytest.approx(1000)
    assert result["weighted_execution_shortfall_bps"] == 0


def test_cached_bps_is_not_used_when_raw_prices_disagree():
    result = summarize_execution_quality([
        observation(execution_shortfall_bps=9999, decision_gap_bps=9999, premium_bps=9999),
    ])
    assert result["weighted_execution_shortfall_bps"] == pytest.approx(100)
    assert result["weighted_decision_gap_bps"] == 0
    assert result["weighted_premium_bps"] == 0


def test_missing_arrival_quote_is_visible_not_replaced_by_limit_price():
    result = summarize_execution_quality([
        observation(arrival_reference_price=None, submitted_price=100, execution_shortfall_bps=0),
        observation(order_id="two"),
    ])
    metric = result["execution_shortfall"]
    assert metric["sample_orders"] == 1
    assert metric["filled_notional_coverage"] == 0.5
    assert result["missing_arrival_reference"] == 1


def test_unfilled_orders_do_not_dilute_slippage_or_become_zero_cost_fills():
    result = summarize_execution_quality([
        observation(), observation(order_id="two", filled_quantity=0, fill_vwap=None),
    ])
    assert result["orders"] == 2
    assert result["filled_orders"] == 1
    assert result["weighted_execution_shortfall_bps"] == pytest.approx(100)
    assert result["execution_shortfall"]["sample_orders"] == 1


def test_timestamp_coverage_is_separate_from_price_coverage():
    result = summarize_execution_quality([
        observation(arrival_reference_time=None, iopv_time=None),
    ])
    assert result["execution_shortfall"]["filled_notional_coverage"] == 1
    assert result["execution_shortfall"]["samples_missing_quote_time"] == 1
    assert result["etf_premium"]["samples_missing_quote_time"] == 1
    assert result["capital_scaling"]["status"] == "DESCRIPTIVE_ONLY"
    assert result["capital_scaling"]["capacity_estimate"] is None
    assert result["capital_scaling"]["automatic_capital_change"] is False


def test_nonfinite_or_nonpositive_prices_are_missing_not_zero_slippage():
    result = summarize_execution_quality([
        observation(fill_vwap=float("nan"), estimated_fees=float("inf")),
        observation(order_id="two", arrival_reference_price=0, iopv=-1),
    ])
    assert result["filled_orders_missing_valid_fill_price"] == 1
    assert result["weighted_execution_shortfall_bps"] is None
    assert result["missing_iopv"] == 1
    json.dumps(result, allow_nan=False)


def test_empty_report_is_not_evidence_of_zero_cost():
    result = summarize_execution_quality([])
    assert result["weighted_execution_shortfall_bps"] is None
    assert result["execution_shortfall"]["filled_notional_coverage"] is None
    assert result["execution_shortfall"]["signed_price_difference_amount"] is None
    assert result["cohorts"] == []


def test_cohorts_keep_symbol_side_and_attempt_separate():
    result = summarize_execution_quality([
        observation(),
        observation(order_id="two", direction="SELL"),
        observation(order_id="three", attempt_id="attempt-b"),
        observation(order_id="four", symbol="159915.SZ", attempt_id=None),
    ])
    assert len(result["cohorts"]) == 4
    assert sum(cohort["observed_orders"] for cohort in result["cohorts"]) == 4


def test_cli_filters_target_and_domain_and_does_not_modify_database(tmp_path, monkeypatch, capsys):
    database = tmp_path / "execution.db"
    url = f"sqlite:///{database}"
    engine = create_engine(url)
    ExecutionQualityObservation.__table__.create(engine)
    with Session(engine) as session:
        session.add_all([
            observation(),
            observation(order_id="paper", execution_domain="paper", fill_vwap=150),
            observation(order_id="other-target", target_id="target-b", fill_vwap=150),
        ])
        session.commit()
    engine.dispose()
    before = database.read_bytes()
    monkeypatch.setattr("sys.argv", [
        "report", "--db-url", url, "--target-id", "target-a", "--execution-domain", "live",
    ])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["orders"] == 1
    assert result["target_id"] == "target-a"
    assert result["execution_domain"] == "live"
    assert result["weighted_execution_shortfall_bps"] == pytest.approx(100)
    assert database.read_bytes() == before
