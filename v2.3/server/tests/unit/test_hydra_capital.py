from app.services.hydra_capital import (
    analyze_capital,
    minimum_capital_for_name_coverage,
)


def test_capital_preflight_reports_lot_coverage_and_weight_error():
    weights = {"A.SH": 0.75, "B.SZ": 0.25}
    prices = {"A.SH": 100.0, "B.SZ": 10.0}
    result = analyze_capital(weights, prices, 20_000, cash_buffer=0.01)
    assert result.shares == {"A.SH": 100, "B.SZ": 400}
    assert result.held_names == 2
    assert result.exposure == 0.7
    assert result.max_abs_weight_error_pp == 25.0


def test_minimum_capital_for_coverage_is_exact_lot_threshold():
    weights = {"A.SH": 0.75, "B.SZ": 0.25}
    prices = {"A.SH": 100.0, "B.SZ": 10.0}
    all_names = minimum_capital_for_name_coverage(
        weights, prices, 1.0, cash_buffer=0.01,
    )
    assert round(all_names, 6) == round(10_000 / (0.75 * 0.99), 6)
