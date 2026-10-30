"""Synthetic counterexamples for the V713 research composite ledger.

Run from the repository root:
    python3 -m pytest tools/test_v713_combination_validation.py -q

These tests contain no market observations or account records. They test
accounting and research semantics, not strategy profitability or live fills.
In particular, passing requires monthly holdings to drift between actual
rebalance dates rather than silently resetting to daily target weights.
"""
from copy import deepcopy
from datetime import date, timedelta
import random

import pytest

from tools.v713_combination_validation import (
    compare,
    lookthrough_weights,
    simulate_periods,
    unique_physical_capital,
    unitize_events,
    paired_scale_sensitivity,
)


def period(start, end, stock=0.0, hydra=0.0, sleeve="T1_5050", known_at=None):
    return {
        "start": start,
        "end": end,
        "known_at": known_at or start,
        "sleeve": sleeve,
        "stock_return": stock,
        "hydra_return": hydra,
    }


def month(index, stock=0.0, hydra=0.0, sleeve="T1_5050"):
    return period(f"2026-{index:02d}-01", f"2026-{index + 1:02d}-01", stock, hydra, sleeve)


def event(day, before, flow, after):
    return {
        "timestamp": f"2026-01-{day:02d}T12:00:00+00:00",
        "value_before_flow": before,
        "external_flow": flow,
        "value_after_flow": after,
    }


def test_unitization_issues_and_redeems_units_without_counting_flows_as_return():
    result = unitize_events([
        event(1, 100, 0, 100),
        event(2, 110, 110, 220),
        event(3, 242, -121, 121),
        event(4, 133.1, 0, 133.1),
    ])
    assert [row["unit_nav"] for row in result] == pytest.approx([1, 1.1, 1.21, 1.331])
    assert [row["units"] for row in result] == pytest.approx([100, 200, 100, 100])


def test_internal_fee_is_a_nav_loss_and_is_not_an_external_withdrawal():
    result = unitize_events([event(1, 100, 0, 100), event(2, 99, 0, 99)])
    assert result[-1]["unit_nav"] == pytest.approx(0.99)
    assert result[-1]["units"] == pytest.approx(100)


@pytest.mark.parametrize("events", [
    [event(1, 100, 10, 100)],
    [event(1, 100, 0, 100), event(1, 100, 0, 100)],
    [event(2, 100, 0, 100), event(1, 100, 0, 100)],
    [event(1, 100, -100, 0)],
    [event(1, 100, -101, -1)],
    [event(1, float("nan"), 0, 100)],
])
def test_invalid_flow_identity_order_or_closed_fund_is_rejected(events):
    with pytest.raises(ValueError):
        unitize_events(events)


def test_monthly_holdings_drift_while_daily_reset_changes_the_result():
    # Stock doubles then halves: unchanged holdings recover exactly. Resetting
    # to 50/50 after the first jump sells some stock and earns a different P&L.
    rows = [period("2026-01-05", "2026-01-06", stock=1.0),
            period("2026-01-06", "2026-01-07", stock=-0.5)]
    monthly = simulate_periods(rows, initial_capital=100, rebalance="monthly")
    state_only = simulate_periods(rows, initial_capital=100, rebalance="state_only")
    daily = simulate_periods(rows, initial_capital=100, rebalance="daily")
    assert monthly["rows"][-1]["nav"] == pytest.approx(100)
    assert state_only["rows"][-1]["nav"] == pytest.approx(100)
    assert daily["rows"][-1]["nav"] == pytest.approx(112.5)
    assert monthly["rows"][1]["start_stock_weight"] == pytest.approx(2 / 3)
    assert monthly["rows"][1]["traded_notional"] == pytest.approx(0)
    assert daily["rows"][1]["traded_notional"] == pytest.approx(50)


def test_monthly_rebalances_at_a_new_month_but_state_only_keeps_units():
    rows = [month(1, stock=1.0), month(2, stock=-0.5)]
    monthly = simulate_periods(rows, initial_capital=100, rebalance="monthly")
    state_only = simulate_periods(rows, initial_capital=100, rebalance="state_only")
    assert monthly["rows"][-1]["nav"] == pytest.approx(112.5)
    assert state_only["rows"][-1]["nav"] == pytest.approx(100)


def test_month_end_rebalance_is_not_repeated_at_the_next_daily_boundary():
    rows = [period("2026-01-29", "2026-01-30", stock=1),
            period("2026-01-30", "2026-02-02", stock=1),
            period("2026-02-02", "2026-02-03", stock=-0.5)]
    result = simulate_periods(rows, initial_capital=100, rebalance="monthly")
    assert result["rows"][1]["start_stock_weight"] == pytest.approx(0.5)
    assert result["rows"][2]["start_stock_weight"] == pytest.approx(2 / 3)
    assert result["rows"][2]["traded_notional"] == pytest.approx(0)
    assert result["rows"][-1]["nav"] == pytest.approx(150)


def test_annualization_uses_input_cadence_rather_than_the_rebalance_rule():
    rows = [period("2026-01-05", "2026-01-06", stock=0.01, hydra=0.01),
            period("2026-01-06", "2026-01-07", stock=0.02, hydra=0.02)]
    cases = [simulate_periods(rows, rebalance=rule, periods_per_year=252)
             for rule in ("monthly", "daily", "state_only")]
    assert [case["summary"]["periods_per_year"] for case in cases] == [252, 252, 252]
    assert cases[0]["summary"]["annualized_period_volatility"] == pytest.approx(
        cases[1]["summary"]["annualized_period_volatility"])
    assert cases[0]["summary"]["cagr_period_based"] == pytest.approx(
        cases[2]["summary"]["cagr_period_based"])
    with pytest.raises(ValueError):
        simulate_periods([month(1), month(2)], rebalance="daily")


def test_comparison_rejects_different_dates_or_frequency():
    left = simulate_periods([month(1)])
    with pytest.raises(ValueError):
        compare(left, simulate_periods([month(2)]))
    other_frequency = deepcopy(left)
    other_frequency["summary"]["periods_per_year"] = 252
    with pytest.raises(ValueError):
        compare(left, other_frequency)


def test_a_state_change_is_effective_at_its_known_boundary_inside_a_month():
    rows = [period("2026-01-05", "2026-01-06", sleeve="TOP50"),
            period("2026-01-06", "2026-01-07", stock=1.0, sleeve="AUX_HYDRA")]
    for rule in ("monthly", "state_only", "daily"):
        result = simulate_periods(rows, initial_capital=100, rebalance=rule)
        assert result["rows"][-1]["nav"] == pytest.approx(100)
        assert result["rows"][-1]["start_stock_weight"] == pytest.approx(0)


@pytest.mark.parametrize("capital,expected_stock", [(200_000, [100_000, 200_000, 0]),
                                                   (400_000, [200_000, 400_000, 0])])
def test_reference_capital_maps_each_state_without_creating_another_real_asset(capital, expected_stock):
    rows = [month(1), month(2, sleeve="TOP50"), month(3, sleeve="AUX_HYDRA")]
    result = simulate_periods(rows, initial_capital=capital)
    for row, expected in zip(result["rows"], expected_stock):
        assert row["start_stock_weight"] * row["nav"] == pytest.approx(expected)
        assert row["stock_scale_to_200k"] == pytest.approx(expected / 200_000)
        assert row["nav"] == pytest.approx(capital)


def test_reference_capital_is_not_reset_to_the_initial_amount_at_a_later_state():
    result = simulate_periods([month(1, stock=1.0, sleeve="TOP50"), month(2)],
                              initial_capital=200_000)
    assert result["rows"][1]["nav"] == pytest.approx(400_000)
    assert result["rows"][1]["stock_scale_to_200k"] == pytest.approx(1.0)
    assert result["rows"][1]["start_hydra_weight"] == pytest.approx(0.5)


def test_rescaling_reference_nav_does_not_claim_scale_specific_lot_or_fee_results():
    # The mathematical fund-unit proxy is homogeneous. Securities-level
    # minimum commissions and round lots require different input return paths.
    rows = [month(1, stock=0.2, hydra=-0.1), month(2, stock=-0.1, hydra=0.1)]
    small = simulate_periods(rows, initial_capital=200_000)
    large = simulate_periods(rows, initial_capital=400_000)
    assert [row["unit_nav"] for row in small["rows"]] == pytest.approx(
        [row["unit_nav"] for row in large["rows"]])
    assert compare(small, large)["relative_terminal_nav"] == pytest.approx(0)


def test_underlying_net_returns_are_not_charged_an_implicit_second_fee():
    result = simulate_periods([month(1, stock=-0.01, sleeve="TOP50"),
                               month(2, stock=0.02, sleeve="TOP50")], initial_capital=100)
    assert result["rows"][-1]["nav"] == pytest.approx(100 * 0.99 * 1.02)
    assert sum(row["cost"] for row in result["rows"]) == pytest.approx(0)


def test_full_switch_has_explicit_buy_and_sell_costs_and_no_invented_cash():
    result = simulate_periods([month(1, sleeve="TOP50"), month(2, sleeve="AUX_HYDRA")],
                              initial_capital=100, switch_bps=100)
    switched = result["rows"][1]
    # Sell 100 with fee 1; only 99 is available for the buy plus its 1% fee.
    purchase = 99 / 1.01
    assert switched["nav"] == pytest.approx(purchase)
    assert switched["traded_notional"] == pytest.approx(100 + purchase)
    assert switched["cost"] == pytest.approx(0.01 * (100 + purchase))
    assert switched["nav"] + switched["cost"] == pytest.approx(100)
    assert switched["cash_weight"] == pytest.approx(0, abs=1e-12)
    assert switched["stock_weight"] == pytest.approx(0)
    # Free seed is an explicit research assumption, not entry-fill evidence.
    assert result["rows"][0]["cost"] == pytest.approx(0)
    assert result["rows"][0]["traded_notional"] == pytest.approx(0)


def test_partial_fill_retains_the_unsold_stock_and_cannot_spend_more_than_sale_proceeds():
    result = simulate_periods([month(1, sleeve="TOP50"), month(2, sleeve="AUX_HYDRA")],
                              initial_capital=100, switch_bps=100, fill_fraction=0.5)
    switched = result["rows"][1]
    purchase = 49.5 / 1.01
    assert switched["stock_weight"] * switched["nav"] == pytest.approx(50)
    assert switched["hydra_weight"] * switched["nav"] == pytest.approx(purchase)
    assert switched["cost"] == pytest.approx(0.01 * (50 + purchase))
    assert switched["traded_notional"] == pytest.approx(50 + purchase)
    assert switched["cash_weight"] == pytest.approx(0, abs=1e-12)
    assert switched["nav"] + switched["cost"] == pytest.approx(100)


def test_zero_fill_preserves_initial_units_and_charges_no_switch_fee():
    result = simulate_periods([month(1, sleeve="TOP50"),
                               month(2, stock=0.1, hydra=-0.5, sleeve="AUX_HYDRA")],
                              initial_capital=100, switch_bps=100, fill_fraction=0)
    assert result["rows"][-1]["nav"] == pytest.approx(110)
    assert result["rows"][-1]["cost"] == pytest.approx(0)
    assert result["rows"][-1]["traded_notional"] == pytest.approx(0)


def test_one_period_lag_keeps_old_exposure_before_a_new_state_is_executed():
    rows = [month(1, sleeve="TOP50"),
            month(2, stock=0.1, hydra=-0.1, sleeve="AUX_HYDRA"),
            month(3, stock=0.2, sleeve="TOP50")]
    current = simulate_periods(rows, initial_capital=100)
    delayed = simulate_periods(rows, initial_capital=100, lag_periods=1)
    assert [row["executed_sleeve"] for row in delayed["rows"]] == ["TOP50", "TOP50", "AUX_HYDRA"]
    assert current["rows"][-1]["nav"] == pytest.approx(108)
    assert delayed["rows"][-1]["nav"] == pytest.approx(110)


def test_half_fund_with_one_percent_internal_cash_has_half_percent_total_cash():
    weights = lookthrough_weights("T1_5050", stock_cash=0.01, hydra_cash=0, outer_cash=0)
    assert weights == pytest.approx({"stocks": 0.495, "hydra_assets": 0.5, "cash": 0.005})
    assert sum(weights.values()) == pytest.approx(1)


def test_adding_one_percent_outer_cash_does_not_erase_already_embedded_cash():
    weights = lookthrough_weights("T1_5050", stock_cash=0.01, hydra_cash=0, outer_cash=0.01)
    assert weights == pytest.approx({"stocks": 0.49005, "hydra_assets": 0.495, "cash": 0.01495})
    assert weights["cash"] != pytest.approx(0.01)


def test_outer_cash_is_a_financed_asset_not_an_extra_addition_to_nav():
    result = simulate_periods([month(1, stock=1, hydra=1)], initial_capital=100, outer_cash_weight=0.1)
    assert result["rows"][0]["nav"] == pytest.approx(190)
    assert result["rows"][0]["cash_weight"] * result["rows"][0]["nav"] == pytest.approx(10)


def test_the_same_physical_hydra_capital_is_counted_once_across_research_books():
    total = unique_physical_capital([
        {"source_id": "physical-stock-sleeve", "capital": 200_000},
        {"source_id": "physical-hydra", "capital": 200_000},
        {"source_id": "physical-hydra", "capital": 200_000},
    ])
    assert total == pytest.approx(400_000)
    with pytest.raises(ValueError):
        unique_physical_capital([
            {"source_id": "physical-hydra", "capital": 200_000},
            {"source_id": "physical-hydra", "capital": 400_000},
        ])


def test_a_signal_observed_after_the_return_interval_starts_is_rejected():
    rows = [period("2026-01-05T09:30:00", "2026-01-05T15:00:00",
                   known_at="2026-01-05T15:00:00")]
    with pytest.raises(ValueError):
        simulate_periods(rows)


def test_future_rows_cannot_rewrite_the_already_computed_prefix():
    prefix = [month(1, stock=0.1, sleeve="TOP50"), month(2, hydra=0.2)]
    alone = simulate_periods(prefix)
    extended = simulate_periods(prefix + [month(3, stock=10, sleeve="AUX_HYDRA")])
    assert extended["rows"][:2] == alone["rows"]


@pytest.mark.parametrize("rows", [
    [],
    [month(1), month(3)],
    [month(2), month(1)],
    [month(1), month(1)],
    [period("2026-01-01", "2026-01-01")],
    [month(1, stock=-1)],
    [month(1, hydra=float("inf"))],
    [month(1, stock=float("nan"))],
    [month(1, sleeve="UNKNOWN")],
])
def test_missing_overlap_nonfinite_or_invalid_periods_are_rejected(rows):
    with pytest.raises(ValueError):
        simulate_periods(rows)


@pytest.mark.parametrize("kwargs", [
    {"initial_capital": 0}, {"switch_bps": -1}, {"fill_fraction": -0.1},
    {"fill_fraction": 1.1}, {"lag_periods": -1}, {"lag_periods": 0.5},
    {"outer_cash_weight": 1.1}, {"fixed_stock_weight": -0.1},
])
def test_impossible_accounting_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        simulate_periods([month(1)], **kwargs)


def test_simulation_does_not_mutate_the_supplied_evidence():
    rows = [month(1, stock=0.1), month(2, hydra=0.2, sleeve="AUX_HYDRA")]
    original = deepcopy(rows)
    simulate_periods(rows, switch_bps=20, fill_fraction=0.5, lag_periods=1)
    assert rows == original


@pytest.mark.parametrize("rule", ["monthly", "state_only", "daily"])
def test_each_period_reconciles_cash_and_actual_unit_changes_under_costs_and_partial_fills(rule):
    # Deterministic invented returns exercise mixed rising/falling legs and
    # residual holdings. This is an accounting stress test, not market data.
    rng = random.Random(713)
    start = date(2026, 1, 1)
    rows = []
    for i in range(90):
        rows.append(period(str(start + timedelta(days=i)), str(start + timedelta(days=i + 1)),
                           stock=rng.uniform(-0.15, 0.15), hydra=rng.uniform(-0.05, 0.05),
                           sleeve=("TOP50", "T1_5050", "AUX_HYDRA")[(i // 11) % 3]))
    rate = 37 / 10_000
    result = simulate_periods(rows, initial_capital=200_000, rebalance=rule,
                              switch_bps=37, fill_fraction=0.35, outer_cash_weight=0.03,
                              lag_periods=2, periods_per_year=252)
    previous_nav, previous_units, prices = 200_000, None, [1.0, 1.0]
    for evidence, actual in zip(rows, result["rows"]):
        stock = actual["pre_return_stock_value"]
        hydra = actual["pre_return_hydra_value"]
        cash = actual["pre_return_cash_value"]
        assert min(stock, hydra, cash) >= -1e-7
        assert stock + hydra + cash + actual["cost"] == pytest.approx(previous_nav)
        assert actual["nav"] == pytest.approx(
            stock * (1 + evidence["stock_return"]) +
            hydra * (1 + evidence["hydra_return"]) + cash)
        assert actual["cost"] == pytest.approx(rate * actual["traded_notional"])
        new_units = [stock / prices[0], hydra / prices[1]]
        if previous_units is not None:
            # Detect any extra liquidation or trade hidden from turnover/fees.
            gross_actual_trade = sum(abs(new_units[i] - previous_units[i]) * prices[i]
                                     for i in range(2))
            assert actual["traded_notional"] == pytest.approx(gross_actual_trade, abs=1e-7)
        previous_units = new_units
        prices = [prices[0] * (1 + evidence["stock_return"]),
                  prices[1] * (1 + evidence["hydra_return"])]
        previous_nav = actual["nav"]
    assert result["summary"]["final_nav"] == pytest.approx(previous_nav)
    assert result["summary"]["max_self_financing_error_over_initial_capital"] < 1e-10


def test_scale_sensitivity_pairs_each_window_before_taking_its_median():
    records = []
    for capital, values in [(100_000, [-0.2, -0.4]), (200_000, [-0.1, -0.5]), (400_000, [-0.1, -0.5])]:
        for index, value in enumerate(values):
            records.append({"scenario": "minimum_5", "capital": capital,
                            "start": str(index), "date": str(index + 12), "relative_nav_deviation": value})
    actual = paired_scale_sensitivity(records)["comparisons"]
    # Both unpaired medians are -30%; subtracting them would wrongly give 0.
    assert actual[0]["median_relative_terminal_nav"] == pytest.approx(((0.8 / 0.9 - 1) + (0.6 / 0.5 - 1)) / 2)
    assert actual[1]["median_relative_terminal_nav"] == pytest.approx(0)
    with pytest.raises(ValueError):
        paired_scale_sensitivity(records[:-1])
