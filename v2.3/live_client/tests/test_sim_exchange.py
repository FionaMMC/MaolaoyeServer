"""Simulated exchange that reproduces the QMT behaviour the OMS depends on."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from live_client.gateway import AccountSnapshot, BrokerOrderSnapshot
from live_client.sim_exchange import SimExchange, SimQMTGateway

CST = timezone(timedelta(hours=8))
D1, D2, D3 = "20261008", "20261009", "20261012"
BASE = {
    "510300.SH": dict(open=4.600, high=4.650, low=4.580, close=4.621, volume=1_000_000),
    "513100.SH": dict(open=2.200, high=2.230, low=2.150, close=2.196, volume=1_000_000),
    "518880.SH": dict(open=9.100, high=9.150, low=9.050, close=9.105, volume=1_000_000),
}


def _bars(overrides=None):
    bars = {(day, symbol): dict(bar) for day in (D1, D2, D3) for symbol, bar in BASE.items()}
    for key, changes in (overrides or {}).items():
        bars[key] = {**bars.get(key, {}), **changes}
    return bars


def _exchange(cash=100_000.0, positions=None, overrides=None, **kwargs) -> SimExchange:
    return SimExchange(_bars(overrides), cash=cash, positions=positions or {}, **kwargs)


def _order(ex: SimExchange, order_id: int) -> dict:
    return next(o for o in ex.orders() if o["order_id"] == order_id)


def test_opening_auction_fills_at_the_official_open_plus_slippage():
    exact = _exchange(slip_bps=0)
    exact.set_clock(D2, "091505")
    order_id = exact.order_stock("513100.SH", "BUY", 600, 2.228, "H000010201")
    assert order_id > 0
    exact.set_clock(D2, "092459")
    assert _order(exact, order_id)["order_status"] == 50 and exact.trades() == []
    exact.set_clock(D2, "092500")
    assert _order(exact, order_id)["order_status"] == 56
    assert [(t["traded_volume"], t["traded_price"]) for t in exact.trades()] == [(600, 2.2)]

    slipped = _exchange()  # 5 bps: 2.200 * 1.0005 = 2.2011 -> next tick up 2.202
    slipped.set_clock(D2, "091505")
    order_id = slipped.order_stock("513100.SH", "BUY", 600, 2.228, "H000010201")
    slipped.set_clock(D2, "093000")
    trade = slipped.trades()[0]
    assert (trade["order_id"], trade["traded_volume"], trade["traded_price"]) == (order_id, 600, 2.202)
    assert _order(slipped, order_id)["traded_price"] == 2.202
    assert slipped.positions()["513100.SH"]["volume"] == 600
    assert slipped.asset()["cash"] == pytest.approx(100_000 - 600 * 2.202 - 5.0)


def test_limit_below_open_fills_at_1455_only_if_the_order_rests_intraday():
    resting = _exchange()
    resting.set_clock(D2, "091505")
    order_id = resting.order_stock("513100.SH", "BUY", 600, 2.180, "H000010201")
    resting.set_clock(D2, "093000")
    assert _order(resting, order_id)["order_status"] == 50  # open 2.202 > limit 2.180
    resting.set_clock(D2, "145459")
    assert resting.trades() == []
    resting.set_clock(D2, "145500")  # day low 2.150 traded below the limit
    assert _order(resting, order_id)["order_status"] == 56
    assert [(t["traded_volume"], t["traded_price"]) for t in resting.trades()] == [(600, 2.180)]

    cancelled = _exchange()
    cancelled.set_clock(D2, "091505")
    order_id = cancelled.order_stock("513100.SH", "BUY", 600, 2.180, "H000010201")
    cancelled.set_clock(D2, "100000")
    assert cancelled.cancel(order_id) == 0
    cancelled.set_clock(D2, "145500")
    assert _order(cancelled, order_id)["order_status"] == 54
    assert cancelled.trades() == []

    untouched = _exchange()  # limit below the day low: never fills
    untouched.set_clock(D2, "091505")
    order_id = untouched.order_stock("513100.SH", "BUY", 600, 2.140, "H000010201")
    untouched.set_clock(D2, "153000")
    assert _order(untouched, order_id)["order_status"] == 50
    assert untouched.trades() == []


def test_closing_auction_fills_at_the_official_close_minus_slippage():
    ex = _exchange(positions={"510300.SH": 3000})
    ex.set_clock(D1, "145705")
    order_id = ex.order_stock("510300.SH", "SELL", 1500, 4.598, "H000010101")
    assert ex.positions()["510300.SH"]["can_use_volume"] == 1500  # frozen by the open sell
    ex.set_clock(D1, "145959")
    assert _order(ex, order_id)["order_status"] == 50
    ex.set_clock(D1, "150000")  # 4.621 * 0.9995 = 4.61869 -> tick down 4.618
    assert _order(ex, order_id)["order_status"] == 56
    assert [(t["traded_volume"], t["traded_price"]) for t in ex.trades()] == [(1500, 4.618)]
    assert ex.positions()["510300.SH"] == {
        "volume": 1500, "can_use_volume": 1500, "market_value": pytest.approx(1500 * 4.621)}
    assert ex.asset()["cash"] == pytest.approx(100_000 + 1500 * 4.618 - 5.0)


def test_unfilled_order_still_shows_status_50_after_the_close():
    ex = _exchange(positions={"510300.SH": 3000})
    ex.set_clock(D1, "145705")
    order_id = ex.order_stock("510300.SH", "SELL", 1500, 4.700, "H000010101")
    ex.set_clock(D1, "150000")
    assert _order(ex, order_id)["order_status"] == 50
    ex.set_clock(D1, "150500")
    assert ex.cancel(order_id) == -1  # market closed
    ex.set_clock(D1, "153000")
    order = _order(ex, order_id)
    assert (order["order_status"], order["traded_volume"]) == (50, 0)
    assert ex.trades() == []


def test_cancel_is_pending_51_until_the_next_clock_advance_then_54():
    ex = _exchange()
    ex.set_clock(D2, "091600")
    order_id = ex.order_stock("513100.SH", "BUY", 600, 2.180, "H000010201")
    frozen = 600 * 2.180 + 5.0
    assert ex.asset()["cash"] == pytest.approx(100_000 - frozen)
    assert ex.asset()["frozen_cash"] == pytest.approx(frozen)
    ex.set_clock(D2, "092100")
    assert ex.cancel(order_id) == -1  # 09:20-09:25 no cancels
    ex.set_clock(D2, "093100")
    assert ex.cancel(order_id) == 0
    assert _order(ex, order_id)["order_status"] == 51
    assert ex.cancel(order_id) == -1  # already pending
    assert ex.asset()["frozen_cash"] == pytest.approx(frozen)  # still frozen while pending
    ex.set_clock(D2, "093100")  # same instant: not an advance
    assert _order(ex, order_id)["order_status"] == 51
    ex.set_clock(D2, "093101")
    assert _order(ex, order_id)["order_status"] == 54
    assert ex.asset()["cash"] == pytest.approx(100_000)
    assert ex.asset()["frozen_cash"] == 0
    assert ex.cancel(order_id) == -1  # terminal

    closing = _exchange(positions={"510300.SH": 1000})
    closing.set_clock(D1, "145705")
    sell_id = closing.order_stock("510300.SH", "SELL", 1000, 4.700, "H000010101")
    closing.set_clock(D1, "145800")
    assert closing.cancel(sell_id) == -1  # 14:57-15:00 no cancels


def test_t_plus_one_shares_bought_today_cannot_be_sold_today():
    ex = _exchange()
    ex.set_clock(D2, "091505")
    ex.order_stock("510300.SH", "BUY", 1000, 4.650, "H000010201")
    ex.set_clock(D2, "093000")
    assert ex.positions()["510300.SH"] == {
        "volume": 1000, "can_use_volume": 0, "market_value": pytest.approx(1000 * 4.600)}
    ex.set_clock(D2, "145705")
    assert ex.order_stock("510300.SH", "SELL", 1000, 4.500, "H000010301") == -1

    ex.set_clock(D3, "145705")
    assert ex.positions()["510300.SH"]["can_use_volume"] == 1000
    assert ex.order_stock("510300.SH", "SELL", 1000, 4.500, "H000010301") > 0


def test_order_remark_is_truncated_to_the_remark_limit():
    ex = _exchange()
    ex.set_clock(D2, "091505")
    remark = "H000010201|abcdefghijklmnopqrstuvwxyz"
    ex.order_stock("513100.SH", "BUY", 100, 2.228, remark)
    ex.order_stock("513100.SH", "BUY", 100, 2.228, "H000010202")
    assert [o["order_remark"] for o in ex.orders()] == [remark[:24], "H000010202"]

    short = _exchange(remark_limit=10)
    short.set_clock(D2, "091505")
    short.order_stock("513100.SH", "BUY", 100, 2.228, remark)
    assert short.orders()[0]["order_remark"] == "H000010201"
    short.set_clock(D2, "093000")
    assert short.trades()[0]["order_remark"] == "H000010201"


def test_queries_only_return_the_current_trading_day():
    ex = _exchange(positions={"510300.SH": 3000})
    gateway = SimQMTGateway(ex, account_id="SIM")
    gateway.connect()
    ex.set_clock(D1, "145705")
    ex.order_stock("510300.SH", "SELL", 1500, 4.598, "H000010101")
    ex.order_stock("510300.SH", "SELL", 1000, 4.700, "H000010102")  # stays 50 after close
    ex.set_clock(D1, "153000")
    assert len(ex.orders()) == 2 and len(ex.trades()) == 1
    assert len(gateway.day_orders()) == 2 and len(gateway.day_trades()) == 1

    ex.set_clock(D2, "090000")
    assert ex.orders() == [] and ex.trades() == []
    assert gateway.day_orders() == [] and gateway.day_trades() == []
    assert ex.positions()["510300.SH"]["can_use_volume"] == 1500  # dead order freed overnight


def test_submit_raises_after_accept_leaves_the_order_at_the_exchange():
    ex = _exchange()
    ex.set_clock(D2, "091505")
    ex.faults.add("submit_raises_after_accept")
    with pytest.raises(RuntimeError):
        ex.order_stock("513100.SH", "BUY", 600, 2.228, "H000010201")
    [order] = ex.orders()
    assert (order["order_remark"], order["order_status"], order["order_volume"]) == ("H000010201", 50, 600)
    assert ex.asset()["frozen_cash"] == pytest.approx(600 * 2.228 + 5.0)

    gateway = SimQMTGateway(ex, account_id="SIM")
    gateway.connect()
    result = gateway.submit_limit(symbol="513100.SH", side="BUY", quantity=300,
                                  limit_price=2.228, remark="H000010202")
    assert result.status == "UNKNOWN" and result.local_order_id is None
    assert [o.order_remark for o in gateway.day_orders()] == ["H000010201", "H000010202"]

    ex.set_clock(D2, "093000")  # the accepted orders trade normally
    assert [o["order_status"] for o in ex.orders()] == [56, 56]


def test_capacity_limit_partially_fills_and_status_stays_55_after_close():
    # 500 lots * 100 shares * 1% participation = 500 shares for the day
    ex = _exchange(overrides={(D2, "513100.SH"): {"volume": 500}})
    ex.set_clock(D2, "091505")
    order_id = ex.order_stock("513100.SH", "BUY", 1200, 2.228, "H000010201")
    ex.set_clock(D2, "093000")
    order = _order(ex, order_id)
    assert (order["order_status"], order["traded_volume"]) == (55, 500)
    assert ex.asset()["frozen_cash"] == pytest.approx(700 * 2.228 + 5.0)
    ex.set_clock(D2, "145500")  # low touched the limit but capacity is used up
    assert _order(ex, order_id)["traded_volume"] == 500
    ex.set_clock(D2, "150000")
    ex.set_clock(D2, "153000")
    order = _order(ex, order_id)
    assert (order["order_status"], order["traded_volume"]) == (55, 500)
    assert sum(t["traded_volume"] for t in ex.trades()) == 500


def test_partial_fill_cancel_goes_52_then_53_and_keeps_the_fill():
    ex = _exchange(overrides={(D2, "513100.SH"): {"volume": 500}})
    ex.set_clock(D2, "091505")
    order_id = ex.order_stock("513100.SH", "BUY", 1200, 2.228, "H000010201")
    ex.set_clock(D2, "145500")
    assert ex.cancel(order_id) == 0
    assert _order(ex, order_id)["order_status"] == 52
    ex.set_clock(D2, "145700")
    order = _order(ex, order_id)
    assert (order["order_status"], order["traded_volume"]) == (53, 500)
    assert ex.asset()["frozen_cash"] == 0


def test_order_entry_validation_returns_minus_one():
    ex = _exchange(cash=1_000.0, positions={"510300.SH": 250})
    assert ex.order_stock("510300.SH", "SELL", 100, 4.6, "x") == -1  # no clock yet
    ex.set_clock(D1, "091400")
    assert ex.order_stock("513100.SH", "BUY", 100, 2.228, "x") == -1  # before 09:15
    ex.set_clock(D1, "091505")
    assert ex.order_stock("513100.SH", "BUY", 150, 2.228, "x") == -1  # odd lot buy
    assert ex.order_stock("513100.SH", "BUY", 500, 2.228, "x") == -1  # 1114 + 5 > 1000
    assert ex.order_stock("513100.SH", "BUY", 100, 2.2285, "x") == -1  # off tick
    assert ex.order_stock("510300.SH", "SELL", 300, 4.6, "x") == -1  # more than sellable
    assert ex.order_stock("510300.SH", "SELL", 120, 4.6, "x") == -1  # odd part must go at once
    assert ex.order_stock("510300.SH", "SELL", 150, 4.6, "x") > 0  # 100 + whole odd 50
    ex.faults.add("submit_minus1")
    assert ex.order_stock("513100.SH", "BUY", 100, 2.228, "x") == -1
    ex.faults.discard("submit_minus1")
    ex.set_clock(D1, "120000")
    assert ex.order_stock("513100.SH", "BUY", 100, 2.228, "x") == -1  # lunch break
    ex.set_clock(D1, "150000")
    assert ex.order_stock("513100.SH", "BUY", 100, 2.228, "x") == -1  # after the close
    ex.set_clock("20261010", "100000")  # Saturday: no bars at all
    assert ex.order_stock("513100.SH", "BUY", 100, 2.228, "x") == -1
    with pytest.raises(ValueError):
        ex.set_clock("20261009", "100000")  # clock never runs backwards


def test_sale_proceeds_can_fund_a_buy_the_same_day():
    ex = _exchange(cash=100.0, positions={"518880.SH": 1000})
    ex.set_clock(D2, "091505")
    assert ex.order_stock("510300.SH", "BUY", 1000, 4.650, "buy") == -1  # no cash yet
    ex.order_stock("518880.SH", "SELL", 1000, 9.000, "sell")  # opening auction sale
    ex.set_clock(D2, "093000")
    proceeds = 1000 * 9.095 - 5.0  # 9.100 * 0.9995 = 9.09545 -> tick down 9.095
    assert ex.asset()["cash"] == pytest.approx(100 + proceeds)
    assert ex.order_stock("510300.SH", "BUY", 1000, 4.650, "buy") > 0


def test_suspended_symbol_order_becomes_junk():
    ex = _exchange(overrides={(D2, "513100.SH"): {"suspendFlag": 1}})
    ex.set_clock(D2, "091505")
    order_id = ex.order_stock("513100.SH", "BUY", 600, 2.228, "H000010201")
    assert order_id > 0
    assert _order(ex, order_id)["order_status"] == 57
    assert ex.asset()["frozen_cash"] == 0
    ex.set_clock(D2, "153000")
    assert ex.trades() == []


def test_clock_jump_to_next_day_still_runs_the_skipped_closing_auction():
    ex = _exchange(positions={"510300.SH": 1500})
    ex.set_clock(D1, "145705")
    ex.order_stock("510300.SH", "SELL", 1500, 4.598, "H000010101")
    ex.set_clock(D2, "090000")
    assert "510300.SH" not in ex.positions()
    assert ex.asset()["cash"] == pytest.approx(100_000 + 1500 * 4.618 - 5.0)


def test_sim_gateway_mirrors_the_xt_gateway_surface():
    ex = _exchange(cash=50_000.0, positions={"510300.SH": 3000})
    gateway = SimQMTGateway(ex, account_id="SIM")
    with pytest.raises(RuntimeError):
        gateway.account_snapshot()  # not connected
    gateway.connect()
    ex.set_clock(D1, "144500")
    snapshot = gateway.account_snapshot()
    assert isinstance(snapshot, AccountSnapshot)
    assert (snapshot.account_id, snapshot.available_cash) == ("SIM", 50_000.0)
    assert snapshot.positions == {"510300.SH": 3000}
    assert snapshot.sellable_positions == {"510300.SH": 3000}
    assert snapshot.total_asset == pytest.approx(50_000 + 3000 * 4.600)

    ex.set_clock(D1, "145705")
    result = gateway.submit_limit(symbol="510300.SH", side="SELL", quantity=1500,
                                  limit_price=4.598, remark="H000010101")
    assert result.status == "SUBMITTED" and int(result.local_order_id) > 0
    [order] = gateway.day_orders()
    assert isinstance(order, BrokerOrderSnapshot)
    assert (order.account_id, order.order_remark, order.strategy_name, order.order_type) == (
        "SIM", "H000010101", "hydra_oms", 24)
    assert gateway.account_snapshot().sellable_positions == {"510300.SH": 1500}
    with pytest.raises(RuntimeError, match="撤单"):
        gateway.cancel_order(999)
    with pytest.raises(RuntimeError, match="撤单"):
        gateway.cancel_order(order.order_id)  # closing auction: no cancels

    ex.set_clock(D1, "150500")
    assert gateway.quotes(["510300.SH", "159915.SZ"]) == {
        "510300.SH": {"last_price": 4.621, "is_trading": True}}
    [trade] = gateway.day_trades()
    assert (trade["side"], trade["quantity"], trade["price"], trade["remark"]) == (
        "SELL", 1500, 4.618, "H000010101")
    assert trade["traded_at"] == datetime(2026, 10, 8, 15, 0, tzinfo=CST).isoformat()
    assert gateway.day_orders()[0].order_status == 56

    ex.faults.add("trades_hang")
    assert gateway.day_trades(timeout_seconds=0.01) is None
    ex.faults.discard("trades_hang")

    ex.faults.add("disconnect_next_call")
    with pytest.raises(RuntimeError, match="disconnect"):
        gateway.day_orders()
    assert len(gateway.day_orders()) == 1  # raised once only
    assert "disconnect_next_call" not in ex.faults

    ex.set_clock(D2, "091505")
    ex.faults.add("submit_minus1")
    rejected = gateway.submit_limit(symbol="510300.SH", side="SELL", quantity=100,
                                    limit_price=4.5, remark="H000010301")
    assert rejected.status == "REJECTED"
    assert gateway.day_orders() == []
