"""Deterministic checks for the stock-leg execution replay (needs v2.3/server on PYTHONPATH)."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

SPEC = importlib.util.spec_from_file_location("stockleg_replay", Path(__file__).with_name("v713_stockleg_execution_replay.py"))
replay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replay  # dataclasses resolve their module by name
SPEC.loader.exec_module(replay)

TEMPLATE = json.loads((Path(__file__).resolve().parents[1] / "v2.3" / "server" / "plugins" / "v713"
                       / "stockleg_policy_template_v1.json").read_text(encoding="utf-8"))
CAL = ["20260930", "20261008", "20261009", "20261012", "20261013", "20261014", "20261015", "20261016",
       "20261019", "20261020", "20261030", "20261102", "20261103", "20261104", "20261105", "20261106"]
A, B = "600001.SH", "600002.SH"


def market(prices=None, bars=None, actions=None, st=()):
    prices = prices or {A: 10.0, B: 20.0}
    table = {(s, d): replay.Bar(open=p, close=p, volume=1e6) for s, p in prices.items() for d in CAL}
    table.update(bars or {})
    return replay.Market(CAL, table, dict(actions or {}), set(st))


def run(periods, end=CAL[-1], data=None, capital=200_000.0):
    return replay.run_window(periods, end=end, capital=capital, template=TEMPLATE, market=data or market())


class RuleTests(unittest.TestCase):
    def test_price_limits_round_half_up_to_the_cent(self):
        self.assertEqual(replay.price_limits(10.0, .1), (11.0, 9.0))
        self.assertEqual(replay.price_limits(10.05, .1), (11.06, 9.05))
        self.assertEqual(replay.price_limits(None, .1), (None, None))

    def test_limit_pct_by_board_st_and_chinext_reform(self):
        self.assertEqual(replay.limit_pct("600001.SH", "20260930", False), .10)
        self.assertEqual(replay.limit_pct("600001.SH", "20260930", True), .05)
        self.assertEqual(replay.limit_pct("688001.SH", "20260930", False), .20)
        self.assertEqual(replay.limit_pct("300001.SZ", "20200821", False), .10)
        self.assertEqual(replay.limit_pct("300001.SZ", "20200824", False), .20)

    def test_auction_fill_blocks_locked_limits_and_suspensions(self):
        bar = replay.Bar(open=11.0, close=9.0, volume=1e5, up_limit=11.0, down_limit=9.0)
        self.assertEqual(replay.auction_fill(bar, "BUY", 11.5), (None, "LIMIT_UP"))
        self.assertEqual(replay.auction_fill(bar, "SELL", 8.5), (None, "LIMIT_DOWN"))
        self.assertEqual(replay.auction_fill(replay.Bar(10.0, 10.0, 0.0), "BUY", 10.5), (None, "SUSPENDED"))
        self.assertEqual(replay.auction_fill(None, "SELL", 9.0), (None, "SUSPENDED"))
        self.assertEqual(replay.auction_fill(replay.Bar(10.2, 9.8, 1e5), "BUY", 10.05), (None, "PRICE"))
        self.assertEqual(replay.auction_fill(replay.Bar(10.0, 9.98, 1e5), "SELL", 9.95), (9.98, None))

    def test_cash_dividend_is_cash_and_bonus_shares_are_floored(self):
        self.assertEqual(replay.apply_corporate_action(1000, 0.2, 0.0), (1000, 200.0))
        self.assertEqual(replay.apply_corporate_action(1000, 0.0, 1.0), (2000, 0.0))
        quantity, cash = replay.apply_corporate_action(333, 0.1, 0.3)
        self.assertEqual(quantity, 432)
        self.assertAlmostEqual(cash, 33.3)

    def test_ex_rights_reference_price(self):
        self.assertAlmostEqual(replay.ex_reference(10.0, 0.5, 0.0, 0.0, 0.0), 9.5)
        self.assertAlmostEqual(replay.ex_reference(10.0, 0.0, 1.0, 0.0, 0.0), 5.0)


class WindowTests(unittest.TestCase):
    def test_build_cycle_from_cash_completes_the_lot_target(self):
        out = run([{"signal": "20260930", "sleeve": "TOP50", "codes": [A, B]}])
        cycle = out["cycles"][0]
        self.assertEqual(cycle["kind"], "BUILD")
        self.assertAlmostEqual(cycle["exec_underweight"], 0.0)
        self.assertAlmostEqual(cycle["first_buy_completion"], 1.0)
        self.assertGreater(cycle["fees"], 0.0)
        self.assertGreater(out["nav"][-1]["stock_weight"], .97)

    def test_aux_month_liquidates_the_whole_leg(self):
        out = run([{"signal": "20260930", "sleeve": "TOP50", "codes": [A, B]},
                   {"signal": "20261030", "sleeve": "AUX_HYDRA", "codes": []}])
        cycle = out["cycles"][1]
        self.assertEqual(cycle["kind"], "AUX_LIQUIDATE")
        self.assertAlmostEqual(cycle["residual_stock_weight"], 0.0)
        self.assertAlmostEqual(out["nav"][-1]["stock_weight"], 0.0)

    def test_limit_down_close_blocks_the_first_sell_and_the_200bp_retry_fills(self):
        data = market()
        anchor = 10.0
        data.bars[(A, "20261102")] = replay.Bar(open=9.0, close=9.0, volume=1e6, down_limit=9.0)
        for day in CAL[CAL.index("20261103"):]:
            data.bars[(A, day)] = replay.Bar(open=anchor * .985, close=anchor * .985, volume=1e6)
        out = run([{"signal": "20260930", "sleeve": "TOP50", "codes": [A, B]},
                   {"signal": "20261030", "sleeve": "AUX_HYDRA", "codes": []}], data=data)
        cycle = out["cycles"][1]
        self.assertTrue(0.0 < cycle["first_sell_completion"] < 1.0)
        self.assertAlmostEqual(cycle["residual_stock_weight"], 0.0)
        reasons = [e["reason"] for e in out["events"] if e["symbol"] == A and e["side"] == "SELL"]
        self.assertIn("LIMIT_DOWN", reasons)

    def test_dividend_on_an_ex_date_is_credited_once_to_the_holder(self):
        plain = run([{"signal": "20260930", "sleeve": "TOP50", "codes": [A, B]}])
        paid = run([{"signal": "20260930", "sleeve": "TOP50", "codes": [A, B]}],
                   data=market(actions={(A, "20261016"): (0.1, 0.0)}))
        actions = [e for e in paid["events"] if e["reason"] == "CORPORATE_ACTION"]
        self.assertEqual(len(actions), 1)
        self.assertGreater(paid["nav"][-1]["cash"], plain["nav"][-1]["cash"])

    def test_name_suspended_on_the_first_sell_day_waits_for_a_later_anchor(self):
        data = market()
        data.bars[(B, "20261008")] = replay.Bar(open=20.0, close=20.0, volume=0.0, suspended=True)
        out = run([{"signal": "20260930", "sleeve": "TOP50", "codes": [A, B]}], data=data)
        reasons = [e["reason"] for e in out["events"] if e["symbol"] == B and e["side"] == "BUY"]
        self.assertEqual(reasons[0], "NO_ANCHOR")
        self.assertAlmostEqual(out["cycles"][0]["exec_underweight"], 0.0)


if __name__ == "__main__":
    unittest.main()
