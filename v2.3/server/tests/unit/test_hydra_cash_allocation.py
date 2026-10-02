"""Fallback allocation uses settled cash, fresh marks and immutable price limits."""
from copy import deepcopy

import pytest

from app.services.hydra_cash_allocation import allocate_buys, replan_residual


def buy(symbol, quantity=900, price=10):
    return dict(symbol=symbol, direction="BUY", quantity=quantity,
                reference_price=price, limit_price=price)


def cost(orders):
    return round(sum(o["quantity"] * o["limit_price"] + max(5, o["quantity"] * o["limit_price"] * .001)
                     for o in orders), 2)


def test_nine_equal_buys_scale_together_to_six_ninths_with_fees():
    orders = [buy(str(i)) for i in range(9)]
    before = deepcopy(orders)
    result = allocate_buys(orders, 9 * 6006, 100, {})
    assert [o["quantity"] for o in result] == [600] * 9
    assert cost(result) <= 9 * 6006
    assert orders == before


def test_discrete_lots_choose_six_deterministically_independent_of_input_order():
    orders = [buy(str(i), 100) for i in range(9)]
    result = allocate_buys(orders, 6030, 100, {})
    assert result == allocate_buys(list(reversed(orders)), 6030, 100, {})
    assert [o["symbol"] for o in result] == list("012345")
    assert cost(result) == 6030


def test_explicit_priority_allocates_before_lower_priority():
    result = allocate_buys([buy("A", 100), buy("Z", 100)], 1005, 100, {"Z": 0, "A": 1})
    assert [o["symbol"] for o in result] == ["Z"]


def test_minimum_fees_and_sub_lot_cash():
    assert allocate_buys([buy("A", 100)], 1004.99, 100, {}) == []
    assert cost(allocate_buys([buy("A", 100)], 1005, 100, {})) == 1005


def replan(**changes):
    kwargs = dict(target_shares={"A": 1000, "B": 1000, "OLD": 0},
                  weights={"A": .5, "B": .5}, cash_buffer_weight=0,
                  actual_positions={"OLD": 1000}, actual_cash=2020,
                  prices={"A": 10., "B": 10., "OLD": 10.},
                  anchors={"A": 10., "B": 10., "OLD": 10.},
                  lot_size=100, priorities={}, blocked_symbols=set())
    kwargs.update(changes)
    return replan_residual(**kwargs)


def test_unfilled_sell_never_funds_replanned_buys():
    orders, audit = replan()
    buys = [o for o in orders if o["direction"] == "BUY"]
    assert {o["symbol"]: o["quantity"] for o in buys} == {"A": 100, "B": 100}
    assert cost(buys) <= 2020
    assert next(o for o in orders if o["direction"] == "SELL")["quantity"] == 1000
    assert audit["cash_budget"] == 2020


def test_one_out_of_envelope_symbol_does_not_block_others():
    orders, audit = replan(prices={"A": 10.06, "B": 10., "OLD": 9.94})
    assert {o["symbol"] for o in orders} == {"B"}
    assert audit["deferred_symbols"]["A"] == "PRICE_GUARD"
    assert audit["deferred_symbols"]["OLD"] == "PRICE_GUARD"


def test_partial_fill_is_deducted_and_fresh_weight_caps_remaining_quantity():
    orders, audit = replan(actual_positions={"A": 400}, actual_cash=3000,
                          prices={"A": 10.04, "B": 10., "OLD": 10.})
    assert all(o["symbol"] != "A" for o in orders)  # already above fresh weight
    assert audit["deferred_symbols"]["A"] == "WEIGHT_CAP"
    assert all(o["direction"] == "BUY" for o in orders)  # no reversal of filled buys
    assert cost(orders) <= 3000


def test_suspension_only_pauses_affected_symbol_and_buffer_is_retained():
    orders, audit = replan(blocked_symbols={"A", "OLD"}, cash_buffer_weight=.1, actual_cash=4010)
    assert {o["symbol"] for o in orders} == {"B"}
    assert cost(orders) <= 4010 - 1401
    assert audit["deferred_symbols"]["A"] == "SUSPENDED_OR_NO_VOLUME"


@pytest.mark.parametrize("cash", [-1, float("nan"), float("inf")])
def test_invalid_cash_fails_closed(cash):
    with pytest.raises(ValueError):
        allocate_buys([buy("A")], cash, 100, {})


def test_varied_prices_lots_and_budgets_never_overspend_or_expand_orders():
    import random
    from decimal import Decimal, ROUND_CEILING
    rng = random.Random(20261002)
    for _ in range(200):
        orders = [buy(str(i), rng.randint(1, 200) * 100, rng.randint(100, 10000) / 1000)
                  for i in range(rng.randint(1, 9))]
        budget = rng.randint(0, 10000000) / 100
        result = allocate_buys(orders, budget, 100, {})
        original = {o['symbol']: o for o in orders}
        spent = 0
        for order in result:
            assert 0 < order['quantity'] <= original[order['symbol']]['quantity']
            assert order['quantity'] % 100 == 0
            notional = Decimal(str(order['limit_price'])) * order['quantity']
            spent += int(((notional + max(Decimal(5), notional / 1000)) * 100)
                         .to_integral_value(rounding=ROUND_CEILING))
        assert spent <= round(budget * 100)
