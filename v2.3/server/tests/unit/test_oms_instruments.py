import json

import pytest

from app.oms.instruments import (InstrumentSpec, buy_cost, estimate_fees, instrument_from_dict,
                                 instrument_to_dict)
from app.oms.planner import (Policy, allocate_buys, get_instrument, lot_target, plan_buy_session,
                             plan_sell_session, policy_from_dict, policy_to_dict)


def stock(**changes):
    values = dict(asset_type="stock", price_tick=.01, buy_lot=100, min_buy_quantity=100,
                  sell_lot=100, odd_lot_sell_all=True, t_plus_one=True, commission_rate=.0003,
                  min_commission=5., stamp_duty_sell=.0005, transfer_fee_rate=.00001,
                  buy_reserve_rate=.0003)
    return InstrumentSpec(**(values | changes))


def policy(specs):
    return Policy(strategy_id="v713", policy_id="V713_TEST_V1", require_instrument_specs=True,
                  instruments={s: instrument_to_dict(v) for s, v in specs.items()}, buy_bps=())


def test_frozen_spec_roundtrip_and_explicit_stock_tick():
    original = policy({"600001.SH": stock()})
    restored = policy_from_dict(json.loads(json.dumps(policy_to_dict(original))))
    assert restored == original
    orders, _ = plan_buy_session(target={"600001.SH": 100}, positions={}, cash=2000.,
                                 buy_anchor={"600001.SH": 10.23}, policy=restored)
    assert orders[0].limit_price == 10.28
    assert orders[0].instrument["asset_type"] == "stock"
    sell, _ = plan_sell_session(target={"600001.SH": 0}, positions={"600001.SH": 100},
                                sellable={"600001.SH": 100}, sell_anchor={"600001.SH": 10.23},
                                attempt=0, policy=restored)
    assert sell[0].limit_price == 10.18


def test_stock_and_etf_fees_are_type_specific_and_budget_includes_fees():
    s = stock(transfer_fee_rate=0.)
    etf = InstrumentSpec("etf", .001, commission_rate=.0003)
    assert estimate_fees(s, 10000, "SELL") == 10.
    assert estimate_fees(etf, 10000, "SELL") == 5.
    assert buy_cost(s, 10., 100) == 1005.
    assert buy_cost(s, 10., 0) == 0.


def test_per_symbol_minimum_and_increments_never_overspend():
    specs = {"600001.SH": stock(), "688001.SH": stock(buy_lot=1, min_buy_quantity=200)}
    orders = [{"symbol": s, "direction": "BUY", "quantity": q, "limit_price": 10.}
              for s, q in (("600001.SH", 300), ("688001.SH", 350))]
    for cash in (0., 100., 2004., 3000., 4500., 6511.):
        allocation = allocate_buys(orders, cash, 100, {}, instruments=specs)
        assert sum(buy_cost(specs[o["symbol"]], o["limit_price"], o["quantity"]) for o in allocation) <= cash
        for order in allocation:
            spec = specs[order["symbol"]]
            assert order["quantity"] >= spec.min_buy_quantity
            assert order["quantity"] % spec.buy_lot == 0
            assert order["quantity"] <= next(o["quantity"] for o in orders if o["symbol"] == order["symbol"])


def test_equity_lot_target_preserves_minimum_and_reports_unbuyable_residual():
    p = policy({"688001.SH": stock(buy_lot=1, min_buy_quantity=200)})
    assert lot_target(1500., {"688001.SH": 1.}, {"688001.SH": 10.}, p) == {"688001.SH": 0}
    orders, deferred = plan_buy_session(target={"688001.SH": 250}, positions={"688001.SH": 100},
                                        cash=5000., buy_anchor={"688001.SH": 10.}, policy=p)
    assert orders == []
    assert deferred[0].quantity == 150 and deferred[0].reason == "BUY_LOT"


def test_removed_stock_liquidates_odd_lot_without_selling_other_strategy_inventory():
    p = policy({"600001.SH": stock()})
    orders, _ = plan_sell_session(target={}, positions={"600001.SH": 153}, sellable={"600001.SH": 153},
                                  sell_anchor={"600001.SH": 10.}, attempt=0, policy=p)
    assert orders[0].quantity == 153
    orders, deferred = plan_sell_session(target={"600001.SH": 100}, positions={"600001.SH": 253},
                                         sellable={"600001.SH": 253}, sell_anchor={"600001.SH": 10.},
                                         attempt=0, policy=p)
    assert orders[0].quantity == 100
    assert deferred[0].quantity == 53 and deferred[0].reason == "SELL_LOT"


def test_missing_instrument_never_treats_equity_as_hydra():
    with pytest.raises(ValueError, match="explicit instrument"):
        get_instrument("600001.SH", Policy())
    with pytest.raises(ValueError, match="explicit instrument"):
        get_instrument("510300.SH", policy({}))


@pytest.mark.parametrize("changes", [{"price_tick": 0}, {"buy_lot": True}, {"min_buy_quantity": 150},
                                    {"commission_rate": float("nan")}, {"buy_reserve_rate": .00001}])
def test_invalid_instrument_contract_is_rejected(changes):
    with pytest.raises(ValueError):
        stock(**changes)


def test_unrecognized_fields_and_out_of_window_submission_are_rejected():
    with pytest.raises(ValueError, match="unknown instrument"):
        instrument_from_dict(instrument_to_dict(stock()) | {"mystery": 1})
    with pytest.raises(ValueError, match="unknown policy"):
        policy_from_dict({"mystery": 1})
    with pytest.raises(ValueError, match="inside"):
        policy_from_dict({"submit_at": {"BUY": "15:00:00", "SELL": "14:57:05"}})


def star(**changes):
    return stock(**({"buy_lot": 1, "min_buy_quantity": 200, "sell_lot": 1, "min_sell_quantity": 200} | changes))


def test_default_min_sell_quantity_is_omitted_so_existing_plans_stay_identical():
    assert "min_sell_quantity" not in instrument_to_dict(stock())
    assert instrument_to_dict(star())["min_sell_quantity"] == 200
    assert instrument_from_dict(instrument_to_dict(star())) == star()
    with pytest.raises(ValueError, match="min_sell_quantity"):
        stock(min_sell_quantity=0)


def test_star_partial_sale_below_200_shares_waits_but_a_full_exit_is_allowed():
    specs = policy({"688001.SH": star()})
    orders, deferrals = plan_sell_session(target={"688001.SH": 200}, positions={"688001.SH": 300},
                                          sellable={"688001.SH": 300}, sell_anchor={"688001.SH": 50.},
                                          attempt=0, policy=specs)
    assert orders == [] and [(d.reason, d.quantity) for d in deferrals] == [("SELL_LOT", 100)]
    orders, _ = plan_sell_session(target={"688001.SH": 200}, positions={"688001.SH": 450},
                                  sellable={"688001.SH": 450}, sell_anchor={"688001.SH": 50.},
                                  attempt=0, policy=specs)
    assert [o.quantity for o in orders] == [250]
    orders, _ = plan_sell_session(target={"688001.SH": 0}, positions={"688001.SH": 150},
                                  sellable={"688001.SH": 150}, sell_anchor={"688001.SH": 50.},
                                  attempt=0, policy=specs)
    assert [o.quantity for o in orders] == [150]


def test_anchor_mode_defaults_to_fixed_and_only_previous_close_is_serialized():
    fixed = Policy()
    assert fixed.anchor == "fixed" and "anchor" not in policy_to_dict(fixed)
    rolling = Policy(strategy_id="v713", policy_id="V713_TEST_V2", anchor="previous_close")
    assert policy_to_dict(rolling)["anchor"] == "previous_close"
    assert policy_from_dict(policy_to_dict(rolling)) == rolling
    with pytest.raises(ValueError, match="anchor"):
        Policy(anchor="intraday")
