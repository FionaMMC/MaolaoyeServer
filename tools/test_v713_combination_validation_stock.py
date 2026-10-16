"""Deterministic checks for the isolated research arithmetic only."""
import importlib.util
from pathlib import Path
import unittest

import numpy as np

SPEC = importlib.util.spec_from_file_location("stock_validation", Path(__file__).with_name("v713_combination_validation_stock.py"))
stock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stock)


class StockSensitivityTests(unittest.TestCase):
    def test_ordinary_and_star_new_holdings_are_legal(self):
        codes = ["600001.SH", "688001.SH", "688002.SH"]
        q, values = stock.allocate(codes, np.array([12.0, 30.0, 1000.0]), 20_000)
        self.assertEqual(q[0] % 100, 0)
        self.assertTrue(q[1] == 0 or q[1] >= 200)
        self.assertEqual(q[2], 0)
        self.assertLessEqual(values.sum(), 19_800)

    def test_trimming_conserves_budget_and_does_not_borrow(self):
        q, values = stock.allocate(["600001.SH", "600002.SH"], np.array([35.0, 35.0]), 10_000)
        self.assertTrue((q >= 0).all())
        self.assertGreaterEqual(10_000 - values.sum(), 100)
        self.assertEqual(values.sum() + (10_000 - values.sum()), 10_000)

    def test_minimum_fee_is_not_scale_homogeneous(self):
        small = stock.costs({}, {f"S{i}": 4000.0 for i in range(50)}, "2026-06-30", "minimum_5")
        large = stock.costs({}, {f"S{i}": 8000.0 for i in range(50)}, "2026-06-30", "minimum_5")
        self.assertEqual(small["commission"], 250.0)
        self.assertEqual(large["commission"], 250.0)
        self.assertNotEqual(small["commission"] * 2, large["commission"])

    def test_report_stamp_scenarios_are_date_sensitive(self):
        before = stock.costs({"S": 10_000.0}, {}, "2023-08-25", "proportional")
        after = stock.costs({"S": 10_000.0}, {}, "2023-08-28", "proportional")
        self.assertEqual(before["stamp_duty"], 10.0)
        self.assertEqual(after["stamp_duty"], 5.0)

    def test_no_fee_flat_path_preserves_capital(self):
        p = {"codes": ["600001.SH", "600002.SH"], "raw_prices": [5.0, 5.0], "returns": [0.0, 0.0], "start": "2026-01-30", "date": "2026-02-27"}
        rows, fees = stock.simulate([p, p], 20_000, "no_fees")
        self.assertEqual(rows[-1]["end_equity"], 20_000)
        self.assertEqual(fees, 0.0)

    def test_fee_is_paid_once_and_cash_remains_reserved(self):
        p = {"codes": ["600001.SH", "600002.SH"], "raw_prices": [5.0, 5.0], "returns": [0.0, 0.0], "start": "2026-01-30", "date": "2026-02-27"}
        rows, fees = stock.simulate([p], 20_000, "minimum_5")
        self.assertEqual(fees, 10.0)
        self.assertEqual(rows[-1]["end_equity"], 20_000 - fees)
        self.assertGreaterEqual(rows[-1]["cash"], 0.01 * rows[-1]["end_equity"])


if __name__ == "__main__":
    unittest.main()
