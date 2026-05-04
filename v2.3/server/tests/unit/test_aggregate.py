"""AggregateService 单元测试"""
from app.services.aggregate import (
    AggregateService, AggregatedOrder, OrderSignalMapping, TaggedSignal,
)
from app.strategy.base import RawSignal


def _ts(signal_id: str, account_group: str = "real_A",
        symbol: str = "600519.SH", direction: str = "BUY",
        qty: int = 100, ref: float = 10.0, off: float = 0.005) -> TaggedSignal:
    return TaggedSignal(
        signal_id=signal_id,
        account_group=account_group,
        raw=RawSignal(symbol=symbol, direction=direction, quantity=qty,
                      reference_price=ref, price_offset=off),
    )


def test_aggregate_empty_returns_empty():
    svc = AggregateService()
    r = svc.aggregate([], valid_date="20260430")
    assert r.orders == []
    assert r.mappings == []


def test_aggregate_single_signal_creates_one_order():
    svc = AggregateService()
    r = svc.aggregate([_ts("s1")], valid_date="20260430")
    assert len(r.orders) == 1
    assert len(r.mappings) == 1
    assert r.orders[0].account_group == "real_A"
    assert r.orders[0].symbol == "600519.SH"
    assert r.orders[0].direction == "BUY"
    assert r.orders[0].quantity == 100
    assert r.orders[0].valid_date == "20260430"
    assert abs(r.orders[0].limit_price - 10.05) < 1e-6
    assert r.mappings[0].signal_id == "s1"
    assert r.mappings[0].signal_quantity == 100
    assert r.mappings[0].order_id == r.orders[0].order_id


def test_aggregate_two_signals_same_key_merges():
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", qty=100, ref=10.0, off=0.005),
        _ts("s2", qty=200, ref=10.0, off=0.005),
    ], valid_date="20260430")
    assert len(r.orders) == 1
    assert r.orders[0].quantity == 300
    assert len(r.mappings) == 2
    assert {m.signal_id for m in r.mappings} == {"s1", "s2"}
    assert all(m.order_id == r.orders[0].order_id for m in r.mappings)


def test_aggregate_buy_takes_max_limit_price():
    """BUY 取最高出价（最激进）。"""
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", qty=100, ref=10.0, off=0.003),
        _ts("s2", qty=100, ref=10.0, off=0.005),
        _ts("s3", qty=100, ref=10.0, off=0.001),
    ], valid_date="20260430")
    assert len(r.orders) == 1
    assert abs(r.orders[0].limit_price - 10.05) < 1e-6


def test_aggregate_sell_takes_min_limit_price():
    """SELL 取最低要价（最激进）。"""
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", direction="SELL", qty=100, ref=10.0, off=-0.001),
        _ts("s2", direction="SELL", qty=100, ref=10.0, off=-0.005),
        _ts("s3", direction="SELL", qty=100, ref=10.0, off=-0.003),
    ], valid_date="20260430")
    assert len(r.orders) == 1
    assert abs(r.orders[0].limit_price - 9.95) < 1e-6


def test_aggregate_different_account_groups_separate():
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", account_group="real_A"),
        _ts("s2", account_group="real_B"),
    ], valid_date="20260430")
    assert len(r.orders) == 2
    groups = {o.account_group for o in r.orders}
    assert groups == {"real_A", "real_B"}


def test_aggregate_different_symbols_separate():
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", symbol="600519.SH"),
        _ts("s2", symbol="000001.SZ"),
    ], valid_date="20260430")
    assert len(r.orders) == 2


def test_aggregate_different_directions_separate():
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", direction="BUY"),
        TaggedSignal(
            signal_id="s2", account_group="real_A",
            raw=RawSignal(symbol="600519.SH", direction="SELL",
                          quantity=100, reference_price=10.0, price_offset=-0.005),
        ),
    ], valid_date="20260430")
    assert len(r.orders) == 2
    dirs = {o.direction for o in r.orders}
    assert dirs == {"BUY", "SELL"}


def test_aggregate_order_id_is_uuid_hex():
    svc = AggregateService()
    r = svc.aggregate([_ts("s1")], valid_date="20260430")
    oid = r.orders[0].order_id
    assert len(oid) == 32
    assert all(c in "0123456789abcdef" for c in oid)


def test_aggregate_complex_scenario():
    """多账户组 + 多标的 + 同标的多策略归集 + 买卖分开。"""
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", account_group="real_A", symbol="600519.SH", qty=100, off=0.003),
        _ts("s2", account_group="real_A", symbol="600519.SH", qty=200, off=0.005),
        TaggedSignal("s3", "real_A", RawSignal(
            symbol="000001.SZ", direction="SELL", quantity=300,
            reference_price=14.0, price_offset=-0.005,
        )),
        _ts("s4", account_group="real_B", symbol="600519.SH", qty=500, off=0.004),
    ], valid_date="20260430")

    assert len(r.orders) == 3
    assert len(r.mappings) == 4

    a_buy = next(o for o in r.orders
                 if o.account_group == "real_A"
                 and o.symbol == "600519.SH"
                 and o.direction == "BUY")
    assert a_buy.quantity == 300
    assert abs(a_buy.limit_price - 10.05) < 1e-6

    a_sell = next(o for o in r.orders
                  if o.account_group == "real_A"
                  and o.symbol == "000001.SZ")
    assert a_sell.quantity == 300
    assert a_sell.direction == "SELL"

    b_buy = next(o for o in r.orders if o.account_group == "real_B")
    assert b_buy.quantity == 500
