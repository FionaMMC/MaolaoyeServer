"""SettlementService + largest_remainder_split 测试"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState, Order, OrderSignalMap, RawSignal, Trade
from app.schemas.trade_result import TradeResult
from app.services.settlement import SettlementService, largest_remainder_split


# ── 拆单纯函数 ────────────────────────────────────────────────────────
def test_split_zero_total():
    assert largest_remainder_split(0, [10, 20, 30]) == [0, 0, 0]


def test_split_zero_weights():
    assert largest_remainder_split(100, [0, 0]) == [0, 0]


def test_split_perfect_division():
    assert largest_remainder_split(300, [100, 200]) == [100, 200]


def test_split_with_remainder():
    """350 split by [100, 200, 300]: floor=[58,116,175] sum=349, remainder=1
    fractional parts: [.33, .67, .0]; max is index 1 → 117"""
    result = largest_remainder_split(350, [100, 200, 300])
    assert sum(result) == 350
    assert result == [58, 117, 175]


def test_split_total_smaller_than_weights():
    result = largest_remainder_split(7, [10, 20, 30])
    assert sum(result) == 7


# ── Service ──────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    return make_session_factory(engine)


def _seed(sf, *, with_signals: bool = True, with_state: bool = True):
    """种一个完整数据：1 个 order，2 条 mapping，2 条 raw_signal，1 个 instance_state。"""
    with sf() as s:
        s.add(Order(
            order_id="oid1", account_group="real_A", symbol="600519.SH",
            direction="BUY", quantity=300, limit_price=10.05,
            valid_date="20260430", status="PENDING", created_at=_now(),
        ))
        s.add(OrderSignalMap(order_id="oid1", signal_id="s1", signal_quantity=100))
        s.add(OrderSignalMap(order_id="oid1", signal_id="s2", signal_quantity=200))

        if with_signals:
            for sid, iid, qty in [("s1", "real_A_m", 100), ("s2", "real_A_r", 200)]:
                s.add(RawSignal(
                    signal_id=sid, instance_id=iid, symbol="600519.SH",
                    direction="BUY", quantity=qty, reference_price=10.0,
                    price_offset=0.005, limit_price=10.05,
                    valid_date="20260430", signal_time=_now(),
                    precheck_status="PASS",
                ))

        if with_state:
            s.add(InstanceState(instance_id="real_A_m", virtual_cash=1_000_000.0,
                                virtual_positions={}, last_update=_now()))
            s.add(InstanceState(instance_id="real_A_r", virtual_cash=2_000_000.0,
                                virtual_positions={}, last_update=_now()))
        s.commit()


def test_settle_unknown_order_in_unmatched(tmp_path: Path):
    sf = _factory(tmp_path)
    svc = SettlementService(session_factory=sf)
    resp = svc.settle("20260430", [
        TradeResult(order_id="ghost", filled_quantity=100, filled_price=10.0,
                    status="FILLED"),
    ])
    assert resp.matched_count == 0
    assert resp.unmatched_order_ids == ["ghost"]


def test_settle_writes_trade_record(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed(sf)
    svc = SettlementService(session_factory=sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.05,
                    filled_time="2026-04-30T09:25:00+08:00", status="FILLED"),
    ])
    with sf() as s:
        trades = s.query(Trade).filter_by(order_id="oid1").all()
        assert len(trades) == 1
        assert trades[0].filled_quantity == 300
        assert trades[0].status == "FILLED"


def test_settle_marks_order_status(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed(sf)
    svc = SettlementService(session_factory=sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=200, filled_price=10.0,
                    status="PARTIAL"),
    ])
    with sf() as s:
        assert s.get(Order, "oid1").status == "PARTIAL"


def test_settle_buy_updates_virtual_state_proportionally(tmp_path: Path):
    """成交 300 股全部 fill；按 100:200 比例拆 → 100/200 给 m/r 实例。"""
    sf = _factory(tmp_path)
    _seed(sf)
    svc = SettlementService(session_factory=sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        r = s.get(InstanceState, "real_A_r")
        # m 拿到 100 股 * 10 = 1000 现金减少
        assert m.virtual_cash == 1_000_000.0 - 1000.0
        assert m.virtual_positions == {"600519.SH": 100}
        # r 拿到 200 股 * 10 = 2000 现金减少
        assert r.virtual_cash == 2_000_000.0 - 2000.0
        assert r.virtual_positions == {"600519.SH": 200}


def test_settle_sell_updates_virtual_state_proportionally(tmp_path: Path):
    """同上但是 SELL：现金增加，持仓减少。"""
    sf = _factory(tmp_path)
    # 改造 seed：order 是 SELL，instance 已经有持仓
    _seed(sf, with_state=False)
    with sf() as s:
        s.get(Order, "oid1").direction = "SELL"
        s.query(RawSignal).filter_by(signal_id="s1").update({"direction": "SELL"})
        s.query(RawSignal).filter_by(signal_id="s2").update({"direction": "SELL"})
        s.add(InstanceState(instance_id="real_A_m", virtual_cash=0.0,
                            virtual_positions={"600519.SH": 100},
                            last_update=_now()))
        s.add(InstanceState(instance_id="real_A_r", virtual_cash=0.0,
                            virtual_positions={"600519.SH": 200},
                            last_update=_now()))
        s.commit()

    svc = SettlementService(session_factory=sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        r = s.get(InstanceState, "real_A_r")
        # m 卖 100 股 → 现金 +1000, 持仓 = 0 → key 应被移除
        assert m.virtual_cash == 1000.0
        assert m.virtual_positions == {}
        # r 卖 200 股 → 现金 +2000, 持仓 = 0 → key 应被移除
        assert r.virtual_cash == 2000.0
        assert r.virtual_positions == {}


def test_settle_zero_filled_skips_state_update(tmp_path: Path):
    """filled_quantity=0 (CANCELLED) 不应改虚拟账本。"""
    sf = _factory(tmp_path)
    _seed(sf)
    svc = SettlementService(session_factory=sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=0, filled_price=0.0,
                    status="CANCELLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        assert m.virtual_cash == 1_000_000.0   # 没变
        assert m.virtual_positions == {}        # 没变


def test_settle_missing_raw_signal_warns_but_continues(tmp_path: Path):
    """raw_signals 缺失不应 crash。"""
    sf = _factory(tmp_path)
    _seed(sf, with_signals=False)
    svc = SettlementService(session_factory=sf)
    resp = svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    assert resp.matched_count == 1   # order 找到了


def test_settle_partial_filled_largest_remainder(tmp_path: Path):
    """成交 350 股（如果 order 实际只下 300 股，这是不太合理的；但测试拆分逻辑）。"""
    sf = _factory(tmp_path)
    _seed(sf)
    svc = SettlementService(session_factory=sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=350, filled_price=10.0,
                    status="FILLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        r = s.get(InstanceState, "real_A_r")
        # weights [100, 200]; 350 split: m→117, r→233
        assert m.virtual_positions["600519.SH"] == 117
        assert r.virtual_positions["600519.SH"] == 233
        # 总合 350 ✓
