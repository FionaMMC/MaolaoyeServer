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


def _make_svc(sf, **fee_overrides):
    """统一构造 SettlementService。默认无费率，保持老测试断言简洁；新增费用相关
    测试显式传入费率。"""
    defaults = dict(commission_rate=0.0, min_commission=0.0, stamp_duty_sell=0.0)
    defaults.update(fee_overrides)
    return SettlementService(session_factory=sf, **defaults)


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
    svc = _make_svc(sf)
    resp = svc.settle("20260430", [
        TradeResult(order_id="ghost", filled_quantity=100, filled_price=10.0,
                    status="FILLED"),
    ])
    assert resp.matched_count == 0
    assert resp.unmatched_order_ids == ["ghost"]


def test_settle_writes_trade_record(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed(sf)
    svc = _make_svc(sf)
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
    svc = _make_svc(sf)
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
    svc = _make_svc(sf)
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

    svc = _make_svc(sf)
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
    svc = _make_svc(sf)
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
    svc = _make_svc(sf)
    resp = svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    assert resp.matched_count == 1   # order 找到了


def test_settle_partial_filled_largest_remainder(tmp_path: Path):
    """成交 350 股（如果 order 实际只下 300 股，这是不太合理的；但测试拆分逻辑）。"""
    sf = _factory(tmp_path)
    _seed(sf)
    svc = _make_svc(sf)
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


def test_settle_buy_rejects_when_cash_insufficient(tmp_path: Path):
    """防穿仓：BUY fill 需要的现金 > virtual_cash，应跳过更新。

    Regression: 5/7 凌晨 dupe orders 把 paper_v20h 现金扣到 -5.4M。修复后，重复
    fill 进来时 settlement 应拒绝，不让账本进一步穿仓。
    """
    sf = _factory(tmp_path)
    _seed(sf)
    # 让 m 实例的现金少到不够买 100 股 × 10 元 = 1000
    with sf() as s:
        s.get(InstanceState, "real_A_m").virtual_cash = 500.0
        s.commit()

    svc = _make_svc(sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        r = s.get(InstanceState, "real_A_r")
        # m 钱不够 → 整个 BUY fill 被忽略；现金不动，没建仓
        assert m.virtual_cash == 500.0
        assert m.virtual_positions == {}
        # r 正常，扣 200 股×10 = 2000
        assert r.virtual_cash == 2_000_000.0 - 2000.0
        assert r.virtual_positions == {"600519.SH": 200}


def test_settle_sell_rejects_when_position_insufficient(tmp_path: Path):
    """防超卖：SELL fill 需要的持仓 > positions[symbol]，应跳过更新。"""
    sf = _factory(tmp_path)
    _seed(sf, with_state=False)
    with sf() as s:
        s.get(Order, "oid1").direction = "SELL"
        s.query(RawSignal).filter_by(signal_id="s1").update({"direction": "SELL"})
        s.query(RawSignal).filter_by(signal_id="s2").update({"direction": "SELL"})
        # m 持仓只有 50 股（不够卖 100），r 正常
        s.add(InstanceState(instance_id="real_A_m", virtual_cash=0.0,
                            virtual_positions={"600519.SH": 50},
                            last_update=_now()))
        s.add(InstanceState(instance_id="real_A_r", virtual_cash=0.0,
                            virtual_positions={"600519.SH": 200},
                            last_update=_now()))
        s.commit()

    svc = _make_svc(sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        r = s.get(InstanceState, "real_A_r")
        # m 持仓不够 → fill 被忽略
        assert m.virtual_cash == 0.0
        assert m.virtual_positions == {"600519.SH": 50}
        # r 正常成交
        assert r.virtual_cash == 2000.0
        assert r.virtual_positions == {"600519.SH": 0} or r.virtual_positions == {}


# ── Bug B: 手续费 + 印花税 ────────────────────────────────────────────────
def test_settle_buy_deducts_commission(tmp_path: Path):
    """BUY 成交：现金消耗 = gross + 佣金（max(min, gross × rate)）。"""
    sf = _factory(tmp_path)
    _seed(sf)
    svc = _make_svc(sf, commission_rate=0.0003, min_commission=5.0)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        r = s.get(InstanceState, "real_A_r")
        # m: 100 股 × 10 = 1000 gross, 佣金 max(5, 1000×0.0003=0.3) = 5
        assert m.virtual_cash == 1_000_000.0 - 1000.0 - 5.0
        # r: 200 股 × 10 = 2000 gross, 佣金 max(5, 2000×0.0003=0.6) = 5
        assert r.virtual_cash == 2_000_000.0 - 2000.0 - 5.0


def test_settle_buy_large_amount_uses_rate_commission(tmp_path: Path):
    """大额 BUY：佣金 = gross × rate（超过 min）"""
    sf = _factory(tmp_path)
    _seed(sf)
    # 修 mapping，让 m 拿到 100 股、r 拿到 200 股，但价格放大到让佣金超过 min
    with sf() as s:
        s.get(Order, "oid1").limit_price = 1000.0
        s.commit()
    svc = _make_svc(sf, commission_rate=0.0003, min_commission=5.0)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=1000.0,
                    status="FILLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        # m: 100 × 1000 = 100000 gross, 佣金 max(5, 100000×0.0003=30) = 30
        assert m.virtual_cash == 1_000_000.0 - 100000.0 - 30.0


def test_settle_sell_deducts_commission_and_stamp_duty(tmp_path: Path):
    """SELL 成交：净收入 = gross - 佣金 - 印花税。"""
    sf = _factory(tmp_path)
    _seed(sf, with_state=False)
    with sf() as s:
        s.get(Order, "oid1").direction = "SELL"
        s.query(RawSignal).filter_by(signal_id="s1").update({"direction": "SELL"})
        s.query(RawSignal).filter_by(signal_id="s2").update({"direction": "SELL"})
        # 用大额，让佣金按 rate 算
        s.get(Order, "oid1").limit_price = 1000.0
        s.add(InstanceState(instance_id="real_A_m", virtual_cash=0.0,
                            virtual_positions={"600519.SH": 100},
                            last_update=_now()))
        s.add(InstanceState(instance_id="real_A_r", virtual_cash=0.0,
                            virtual_positions={"600519.SH": 200},
                            last_update=_now()))
        s.commit()

    svc = _make_svc(sf, commission_rate=0.0003, min_commission=5.0,
                   stamp_duty_sell=0.0005)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=1000.0,
                    status="FILLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        # m: 100 × 1000 = 100000 gross
        # 佣金 max(5, 100000×0.0003=30) = 30, 印花税 100000×0.0005 = 50
        # 净收入 = 100000 - 30 - 50 = 99920
        assert m.virtual_cash == 99920.0


# ── Bug C: 防穿仓 / 防超卖触发后，order.bookkeeping_divergence = True ──────
def test_settle_buy_insufficient_cash_flags_divergence(tmp_path: Path):
    """虚拟现金不够时跳过账本更新，但 order.bookkeeping_divergence 被置 True，
    方便人工对账。"""
    sf = _factory(tmp_path)
    _seed(sf)
    with sf() as s:
        s.get(InstanceState, "real_A_m").virtual_cash = 500.0
        s.commit()

    svc = _make_svc(sf, commission_rate=0.0003, min_commission=5.0)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    with sf() as s:
        order = s.get(Order, "oid1")
        # order 状态仍是 FILLED（QMT 真实成交了）
        assert order.status == "FILLED"
        # 但 bookkeeping_divergence 被置 True
        assert order.bookkeeping_divergence is True


def test_settle_sell_insufficient_position_flags_divergence(tmp_path: Path):
    """虚拟持仓不够卖 → 跳过账本 + 标记 divergence。"""
    sf = _factory(tmp_path)
    _seed(sf, with_state=False)
    with sf() as s:
        s.get(Order, "oid1").direction = "SELL"
        s.query(RawSignal).filter_by(signal_id="s1").update({"direction": "SELL"})
        s.query(RawSignal).filter_by(signal_id="s2").update({"direction": "SELL"})
        s.add(InstanceState(instance_id="real_A_m", virtual_cash=0.0,
                            virtual_positions={"600519.SH": 50},  # 只有 50 股
                            last_update=_now()))
        s.add(InstanceState(instance_id="real_A_r", virtual_cash=0.0,
                            virtual_positions={"600519.SH": 200},
                            last_update=_now()))
        s.commit()

    svc = _make_svc(sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    with sf() as s:
        order = s.get(Order, "oid1")
        assert order.bookkeeping_divergence is True


def test_settle_normal_fill_no_divergence(tmp_path: Path):
    """正常成交，bookkeeping_divergence 保持 False。"""
    sf = _factory(tmp_path)
    _seed(sf)
    svc = _make_svc(sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    with sf() as s:
        order = s.get(Order, "oid1")
        assert order.bookkeeping_divergence is False


# ── P0-1: 成交回报幂等（5/12 重复推送事故回归）──────────────────────────────
def test_settle_duplicate_fill_is_idempotent(tmp_path: Path):
    """重复推送同一笔成交回报（客户端网络重试 / 全量重推）必须是 no-op。

    Regression: 5/12 客户端隔 35 分钟重推同一批回报。settle() 当时无幂等守卫，
    导致 22 笔订单 Σ成交量 = 2× 委托量，虚拟账本现金/持仓被双重应用，静默腐蚀
    （余量足够时连 bookkeeping_divergence 都不触发）。
    """
    sf = _factory(tmp_path)
    _seed(sf)
    svc = _make_svc(sf)
    fill = TradeResult(
        order_id="oid1", filled_quantity=300, filled_price=10.0,
        filled_time="2026-04-30T09:25:00+08:00", status="FILLED",
    )
    svc.settle("20260430", [fill])   # 第一次
    svc.settle("20260430", [fill])   # 第二次：完全相同 → 必须 no-op

    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        r = s.get(InstanceState, "real_A_r")
        # 账本只移动一次（不是双倍）
        assert m.virtual_cash == 1_000_000.0 - 1000.0
        assert m.virtual_positions == {"600519.SH": 100}
        assert r.virtual_cash == 2_000_000.0 - 2000.0
        assert r.virtual_positions == {"600519.SH": 200}
        # trades 表不重复记录这笔成交
        assert len(s.query(Trade).filter_by(order_id="oid1").all()) == 1


def test_settle_distinct_partial_fills_both_apply(tmp_path: Path):
    """同一 order 的两笔【不同】部分成交（filled_time/数量不同）都必须入账。

    保证幂等去重不会误杀合法的分笔成交（防过度去重）。
    """
    sf = _factory(tmp_path)
    _seed(sf)
    svc = _make_svc(sf)
    # 早盘成交 100 股
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=100, filled_price=10.0,
                    filled_time="2026-04-30T09:30:00+08:00", status="PARTIAL"),
    ])
    # 午盘成交剩余 200 股（不同 filled_time + 不同数量）
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=200, filled_price=10.0,
                    filled_time="2026-04-30T13:00:00+08:00", status="FILLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        r = s.get(InstanceState, "real_A_r")
        # 两笔都入账：m 累计拿到 100 股，r 累计 200 股（总 300）
        assert m.virtual_positions.get("600519.SH", 0) == 100
        assert r.virtual_positions.get("600519.SH", 0) == 200
        # trades 表有两条不同记录
        assert len(s.query(Trade).filter_by(order_id="oid1").all()) == 2
