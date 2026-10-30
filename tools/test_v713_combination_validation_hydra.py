"""Synthetic alignment gates; no remote, price, account, or broker inputs."""
from copy import deepcopy

import pandas as pd
import pytest

from tools.v713_combination_validation_hydra import aligned_periods


def inputs():
    calendar = pd.DatetimeIndex(["2026-01-30", "2026-02-02", "2026-02-03", "2026-02-04"])
    # The large left-boundary return must NOT enter the (start, end] product.
    original = pd.Series([99.0, 0.10, -0.10, 0.02], index=calendar)
    rb = pd.Series([99.0, 0.03, -0.02, 0.01], index=calendar)
    stock = pd.DataFrame([{
        "start": "2026-01-30", "date": "2026-02-04", "known_at": "2026-01-30",
        "sleeve": "T1_5050", "top50_return": 0.2, "stock_return": 0.19, "cost": 0.01,
    }])
    return stock, original, rb, calendar


def test_complete_period_compounds_every_daily_return_once_and_preserves_source_variants():
    stock, original, rb, calendar = inputs()
    old_stock, old_original, old_rb = deepcopy(stock), original.copy(), rb.copy()
    rows, excluded = aligned_periods(stock, original, rb, calendar)
    assert excluded == []
    assert len(rows) == 1
    row = rows[0]
    hydra_return = 1.10 * 0.90 * 1.02 - 1
    rb_return = 1.03 * 0.98 * 1.01 - 1
    assert row["hydra_return"] == pytest.approx(hydra_return)
    assert row["rb_hydra_return"] == pytest.approx(rb_return)
    assert row["observed_daily_rows"] == 3
    assert row["stock_return"] == pytest.approx(0.2)
    assert row["stock_net_proxy_return"] == pytest.approx(0.19)
    assert row["stock_carry_valuation_return"] == pytest.approx(0.19)
    assert row["original_baseline_gross_return"] == pytest.approx(0.5 * 0.2 + 0.5 * hydra_return)
    assert row["original_baseline_net_return"] == pytest.approx(0.5 * 0.2 + 0.5 * hydra_return - 0.01)
    pd.testing.assert_frame_equal(stock, old_stock)
    pd.testing.assert_series_equal(original, old_original)
    pd.testing.assert_series_equal(rb, old_rb)


def test_both_series_missing_the_same_trading_day_are_not_accepted_as_complete():
    stock, original, rb, calendar = inputs()
    missing = pd.Timestamp("2026-02-03")
    rows, excluded = aligned_periods(stock, original.drop(missing), rb.drop(missing), calendar)
    assert rows == []
    assert excluded == [{"start": "2026-01-30", "end": "2026-02-04",
                         "reason": "shared_returns_incomplete_against_stock_calendar"}]


@pytest.mark.parametrize("boundary", ["2026-01-30", "2026-02-04"])
def test_unobserved_period_boundary_is_excluded_instead_of_inventing_a_valuation(boundary):
    stock, original, rb, calendar = inputs()
    rows, excluded = aligned_periods(stock, original.drop(pd.Timestamp(boundary)), rb, calendar)
    assert rows == []
    assert excluded[0]["reason"] == "boundary_outside_observed_daily_calendar"


@pytest.mark.parametrize("changes", [
    {"known_at": "2026-02-02"},
    {"sleeve": "UNKNOWN"},
    {"start": "2026-01-30T09:30:00", "known_at": "2026-01-30T15:00:00"},
    # This date-only aligner cannot certify intraday intervals even when the
    # signal is known in time; callers must use a suitable intraday pipeline.
    {"start": "2026-01-30T15:00:00", "known_at": "2026-01-30T09:30:00"},
])
def test_unknown_state_future_signal_or_silently_truncated_intraday_input_is_rejected(changes):
    stock, original, rb, calendar = inputs()
    for key, value in changes.items():
        stock.loc[0, key] = value
    with pytest.raises(ValueError):
        aligned_periods(stock, original, rb, calendar)
