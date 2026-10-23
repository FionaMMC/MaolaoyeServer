"""Deterministic checks for the share-count fee ledger and no-trade band."""
import importlib.util
from pathlib import Path
import unittest

import numpy as np


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stock = load("stock_validation", "v713_combination_validation_stock.py")
share = load("share_delta", "v713_scale_share_delta.py")


def period(codes, prices, returns, start="2026-01-30", date="2026-02-27"):
    return {"codes": codes, "raw_prices": prices, "returns": returns, "start": start, "date": date}


# A 0.01% residual between the adjusted return and the raw price path, as
# from a small cash dividend or rounded adjusted prices.
CODES = ["600001.SH", "600002.SH"]
FIRST = period(CODES, [10.0, 10.0], [0.1001, 0.1001])
SECOND = period(CODES, [11.0, 11.0], [0.0, 0.0], "2026-02-27", "2026-03-31")


class CarryTests(unittest.TestCase):
    def test_cash_dividend_stays_cash_and_keeps_share_count(self):
        held, cash = share.carry(1000, 1000 * 10.0 * 1.02, 10.0)
        self.assertEqual(held, 1000)
        self.assertAlmostEqual(cash, 200.0)

    def test_bonus_shares_raise_share_count(self):
        held, cash = share.carry(1000, 10_000.0, 5.0)
        self.assertEqual(held, 2000)
        self.assertAlmostEqual(cash, 0.0)


class OrderCountTests(unittest.TestCase):
    def test_value_delta_model_charges_unchanged_holding(self):
        rows, _ = stock.simulate([FIRST, SECOND], 100_000, "minimum_5")
        self.assertAlmostEqual(rows[1]["fee"], 10.0, places=2)

    def test_unchanged_share_count_places_no_order(self):
        rows, _ = share.simulate_shares([FIRST, SECOND], 100_000, "minimum_5", band=0.0)
        self.assertEqual(rows[1]["orders"], 0)
        self.assertEqual(rows[1]["fee"], 0.0)


class BandTests(unittest.TestCase):
    CODES4 = ["600001.SH", "600002.SH", "600003.SH", "600004.SH"]
    PRICES4 = np.array([10.0] * 4)
    # Desired value per name is 0.99 * 102000 / 4 = 25245; every target is 2500.
    HELD = {"600001.SH": 2400, "600002.SH": 3500, "600003.SH": 1500}
    EQUITY = 102_000.0

    def test_band_keeps_small_drift_and_trades_large_drift(self):
        q, fee = share.rebalance(self.CODES4, self.PRICES4, self.HELD, {}, self.EQUITY, 0.25, "minimum_5", "2026-02-27")
        self.assertEqual(q.tolist(), [2400, 2500, 2500, 2500])
        self.assertEqual(fee["economic_orders"], 3)

    def test_zero_band_trades_every_changed_share_count(self):
        q, fee = share.rebalance(self.CODES4, self.PRICES4, self.HELD, {}, self.EQUITY, 0.0, "minimum_5", "2026-02-27")
        self.assertEqual(q.tolist(), [2500, 2500, 2500, 2500])
        self.assertEqual(fee["economic_orders"], 4)

    def test_exiting_holding_is_sold_in_full(self):
        q, fee = share.rebalance(["600001.SH"], np.array([10.0]), {}, {"600009.SH": 10_000.0}, 10_000.0, 0.25, "proportional", "2026-02-27")
        self.assertEqual(fee["sell_value"], 10_000.0)
        self.assertEqual(fee["stamp_duty"], 5.0)
        self.assertEqual(fee["economic_orders"], 2)

    def test_cash_reserve_holds_when_kept_holdings_are_overweight(self):
        prices = np.array([10.0, 10.0])
        q, fee = share.rebalance(CODES, prices, {"600001.SH": 5200, "600002.SH": 4800}, {}, 100_000.0, 0.25, "minimum_5", "2026-02-27")
        cash = 100_000.0 - (q * prices).sum() - fee["total"]
        self.assertGreaterEqual(cash, 0.01 * (100_000.0 - fee["total"]))
        self.assertLess(q[0], 5200)
        self.assertEqual(q[1], 4800)


class PathTests(unittest.TestCase):
    def test_flat_path_conserves_capital_less_fees(self):
        flat = [period(CODES, [5.0, 5.0], [0.0, 0.0], f"2026-0{m}-27", f"2026-0{m + 1}-27") for m in (1, 2, 3)]
        rows, fees = share.simulate_shares(flat, 20_000, "minimum_5", band=0.25)
        self.assertAlmostEqual(rows[-1]["end_equity"], 20_000 - fees)
        self.assertEqual([r["orders"] for r in rows], [2, 0, 0])
        self.assertGreaterEqual(rows[-1]["cash"], 0.01 * rows[-1]["end_equity"])


if __name__ == "__main__":
    unittest.main()
