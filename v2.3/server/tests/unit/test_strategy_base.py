"""Strategy 基类 + RawSignal 测试"""
import pytest

from app.strategy.base import RawSignal, Strategy


def test_raw_signal_validates_direction():
    with pytest.raises(ValueError, match="direction"):
        RawSignal(symbol="600519.SH", direction="HOLD", quantity=100,
                  reference_price=10.0, price_offset=0.005)


def test_raw_signal_validates_quantity():
    with pytest.raises(ValueError, match="quantity"):
        RawSignal(symbol="600519.SH", direction="BUY", quantity=0,
                  reference_price=10.0, price_offset=0.005)


def test_raw_signal_validates_reference_price():
    with pytest.raises(ValueError, match="reference_price"):
        RawSignal(symbol="600519.SH", direction="BUY", quantity=100,
                  reference_price=-1.0, price_offset=0.005)


def test_raw_signal_immutable():
    s = RawSignal(symbol="A.SH", direction="BUY", quantity=100,
                  reference_price=10.0, price_offset=0.005)
    with pytest.raises(Exception):
        s.symbol = "B.SH"


def test_strategy_is_abstract():
    with pytest.raises(TypeError):
        Strategy()


def test_strategy_subclass_must_implement_run():
    class IncompleteStrategy(Strategy):
        name = "incomplete"
    with pytest.raises(TypeError):
        IncompleteStrategy()


def test_strategy_subclass_works():
    class GoodStrategy(Strategy):
        name = "good"
        def run(self, ctx, trade_date):
            return []
    s = GoodStrategy()
    assert s.name == "good"
    assert s.run(None, 20260430) == []
