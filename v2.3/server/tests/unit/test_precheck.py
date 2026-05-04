"""PrecheckService 单元测试"""
import pytest

from app.services.precheck import PrecheckResult, PrecheckService
from app.strategy.base import RawSignal


def _buy(qty: int = 100, price: float = 10.0, offset: float = 0.005) -> RawSignal:
    return RawSignal(symbol="600519.SH", direction="BUY", quantity=qty,
                     reference_price=price, price_offset=offset)


def _sell(qty: int = 100, price: float = 10.0, offset: float = -0.005,
          symbol: str = "600519.SH") -> RawSignal:
    return RawSignal(symbol=symbol, direction="SELL", quantity=qty,
                     reference_price=price, price_offset=offset)


def test_invalid_fee_rate_raises():
    with pytest.raises(ValueError, match="fee_rate"):
        PrecheckService(fee_rate=-0.01)


def test_buy_pass_with_enough_cash():
    svc = PrecheckService(fee_rate=0.0)
    r = svc.check(_buy(qty=100, price=10.0, offset=0.005),
                  virtual_cash=10000.0, virtual_positions={})
    assert r.status == "PASS"
    assert r.reason is None


def test_buy_fail_insufficient_cash():
    svc = PrecheckService(fee_rate=0.001)
    r = svc.check(_buy(qty=100, price=10.0, offset=0.005),
                  virtual_cash=1000.0, virtual_positions={})
    assert r.status == "FAIL"
    assert "cash" in (r.reason or "")


def test_buy_fail_quantity_not_100_multiple():
    svc = PrecheckService(fee_rate=0.0)
    r = svc.check(_buy(qty=150, price=10.0),
                  virtual_cash=10_000_000.0, virtual_positions={})
    assert r.status == "FAIL"
    assert "100 整数倍" in (r.reason or "")


def test_buy_includes_fee_in_cost():
    """手续费要计入资金检查。"""
    svc = PrecheckService(fee_rate=0.001)
    r = svc.check(_buy(qty=100, price=10.0, offset=0.0),
                  virtual_cash=1000.5, virtual_positions={})
    assert r.status == "FAIL"


def test_buy_offset_increases_cost():
    """price_offset 应该被算进 limit_price。"""
    svc = PrecheckService(fee_rate=0.0)
    r_fail = svc.check(_buy(qty=100, price=10.0, offset=0.005),
                       virtual_cash=1004.0, virtual_positions={})
    r_pass = svc.check(_buy(qty=100, price=10.0, offset=0.005),
                       virtual_cash=1005.0, virtual_positions={})
    assert r_fail.status == "FAIL"
    assert r_pass.status == "PASS"


def test_sell_pass_with_enough_position():
    svc = PrecheckService(fee_rate=0.0)
    r = svc.check(_sell(qty=100),
                  virtual_cash=0.0, virtual_positions={"600519.SH": 200})
    assert r.status == "PASS"


def test_sell_fail_insufficient_position():
    svc = PrecheckService()
    r = svc.check(_sell(qty=200),
                  virtual_cash=0.0, virtual_positions={"600519.SH": 100})
    assert r.status == "FAIL"
    assert "position" in (r.reason or "")


def test_sell_fail_no_position_at_all():
    svc = PrecheckService()
    r = svc.check(_sell(qty=100),
                  virtual_cash=0.0, virtual_positions={})
    assert r.status == "FAIL"


def test_sell_clearance_allows_non_100_multiple():
    """清仓尾单：quantity == held 时允许非整百。"""
    svc = PrecheckService()
    r = svc.check(_sell(qty=350),
                  virtual_cash=0.0, virtual_positions={"600519.SH": 350})
    assert r.status == "PASS"


def test_sell_partial_must_be_100_multiple():
    svc = PrecheckService()
    r = svc.check(_sell(qty=150),
                  virtual_cash=0.0, virtual_positions={"600519.SH": 350})
    assert r.status == "FAIL"
    assert "100 整数倍" in (r.reason or "")


def test_sell_partial_100_multiple_passes():
    svc = PrecheckService()
    r = svc.check(_sell(qty=200),
                  virtual_cash=0.0, virtual_positions={"600519.SH": 350})
    assert r.status == "PASS"
