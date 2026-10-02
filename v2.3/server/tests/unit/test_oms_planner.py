from app.oms.planner import (Policy, Session, buy_limit, lot_target, plan_buy_session,
                             plan_sell_session, sell_limit, session_schedule)

P = Policy()


def test_limits_round_to_tick_away_from_chasing():
    assert buy_limit(2.196, 150) == 2.228
    assert buy_limit(9.105, 100) == 9.196
    assert sell_limit(4.621, 50) == 4.598
    assert sell_limit(4.621, 200) == 4.529


def test_nearest_lot_rounds_up_only_when_it_cuts_error_and_fits():
    prices = {"511260.SH": 136.0, "510300.SH": 4.6}
    t = lot_target(200000.0, {"511260.SH": 0.8, "510300.SH": 0.2}, prices, P)
    assert t["511260.SH"] % 100 == 0 and t["510300.SH"] % 100 == 0
    cost = sum(q * prices[s] for s, q in t.items()) * P.size_factor
    assert cost <= 200000.0
    assert t["511260.SH"] == 1100      # 160000/136/1.001 = 11.75 lots -> 11; a 12th lot would overspend the basket
    assert t["510300.SH"] == 8700      # 86.87 lots -> 86, +1 lot cuts the error (440 of 460) and still fits


def test_sell_session_caps_at_sellable_and_uses_attempt_band():
    orders, deferrals = plan_sell_session(
        target={"510300.SH": 1000, "518880.SH": 0}, positions={"510300.SH": 3000, "518880.SH": 800},
        sellable={"510300.SH": 1500, "518880.SH": 800}, sell_anchor={"510300.SH": 4.621, "518880.SH": 9.105},
        attempt=1, policy=P)
    by = {o.symbol: o for o in orders}
    assert by["510300.SH"].quantity == 1500 and by["510300.SH"].limit_price == 4.529
    assert by["518880.SH"].quantity == 800
    assert deferrals[0].symbol == "510300.SH" and deferrals[0].reason == "NOT_SELLABLE"
    assert deferrals[0].quantity == 500


def test_buy_session_allocates_cash_and_skips_untradable():
    orders, deferrals = plan_buy_session(
        target={"513100.SH": 1000, "159915.SZ": 400}, positions={}, cash=1500.0,
        buy_anchor={"513100.SH": 2.196, "159915.SZ": 3.330}, tradable={"513100.SH"}, policy=P)
    assert [o.symbol for o in orders] == ["513100.SH"]
    assert orders[0].quantity == 600 and orders[0].limit_price == 2.228
    reasons = {(d.symbol, d.reason) for d in deferrals}
    assert ("159915.SZ", "SUSPENDED") in reasons and ("513100.SH", "CASH") in reasons


def test_october_schedule_matches_design_table():
    cal = ["20260929", "20260930", "20261008", "20261009", "20261012", "20261013", "20261014", "20261015"]
    assert session_schedule(cal, "20260930", 3) == [
        Session(1, "20261008", "SELL", 0), Session(2, "20261009", "BUY", None),
        Session(3, "20261012", "SELL", 1), Session(4, "20261013", "BUY", None)]
