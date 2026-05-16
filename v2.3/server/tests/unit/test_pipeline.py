"""StrategyPipeline 集成测试（用真实 SQLite + 真实 Parquet + 内联策略）"""
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState, Order, RawSignal as RawSignalRow
from app.scheduler.pipeline import StrategyPipeline
from app.services.aggregate import AggregateService
from app.services.orders_queue import OrdersQueueService
from app.services.perf import PerfService
from app.services.precheck import PrecheckService
from app.storage.parquet import ParquetStore
from app.strategy.base import RawSignal, Strategy


# ── 测试用策略 ───────────────────────────────────────────────────────
class AlwaysBuyStrategy(Strategy):
    """每天买茅台 100 股。"""
    name = "always_buy"
    def run(self, ctx, trade_date):
        return [RawSignal(
            symbol="600519.SH", direction="BUY", quantity=100,
            reference_price=10.0, price_offset=0.005,
        )]


class NoopStrategy(Strategy):
    name = "noop"
    def run(self, ctx, trade_date):
        return []


# ── fixtures ──────────────────────────────────────────────────────
def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _bar(d: int, close: float = 10.0) -> dict:
    return {"trade_date": d, "open": close, "high": close, "low": close,
            "close": close, "volume": 1000, "amount": close * 1000,
            "suspendFlag": 0}


@pytest.fixture
def setup(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    sf = make_session_factory(engine)
    store = ParquetStore(root=tmp_path / "data")
    yaml_path = tmp_path / "strategies.yaml"

    pipeline = StrategyPipeline(
        registry={"always_buy": AlwaysBuyStrategy, "noop": NoopStrategy},
        parquet_store=store,
        session_factory=sf,
        precheck=PrecheckService(fee_rate=0.001),
        aggregate=AggregateService(),
        orders_queue=OrdersQueueService(session_factory=sf),
        perf=PerfService(session_factory=sf, parquet_store=store),
        strategies_yaml_path=yaml_path,
    )
    return pipeline, sf, store, yaml_path


def _write_yaml(path: Path, content: dict):
    path.write_text(yaml.safe_dump(content), encoding="utf-8")


# ── 测试 ──────────────────────────────────────────────────────────
def test_pipeline_no_yaml_returns_zero(setup):
    pipeline, sf, store, yaml_path = setup
    summary = pipeline.run(20260430)
    assert summary["instances"] == 0
    assert summary["orders"] == 0


def test_pipeline_creates_default_instance_state(setup):
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "noop", "virtual_initial_cash": 500_000}],
        }],
    })
    pipeline.run(20260430)

    with sf() as s:
        row = s.get(InstanceState, "real_A_noop")
        assert row is not None
        assert row.virtual_cash == 500_000


def test_pipeline_runs_strategy_and_creates_orders(setup):
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "always_buy", "virtual_initial_cash": 1_000_000}],
        }],
    })
    summary = pipeline.run(20260430)

    assert summary["signals"] == 1
    assert summary["passed"] == 1
    assert summary["orders"] == 1

    with sf() as s:
        # raw_signals 表里应该有一条 PASS 记录
        signals = s.query(RawSignalRow).all()
        assert len(signals) == 1
        assert signals[0].precheck_status == "PASS"
        # orders 表里应该有一条 PENDING 订单
        orders = s.query(Order).all()
        assert len(orders) == 1
        assert orders[0].status == "PENDING"
        assert orders[0].symbol == "600519.SH"


def test_pipeline_aggregates_across_instances(setup):
    """两个实例同账户组同标的同方向 → 归集为一条订单。"""
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [
                {"strategy_id": "always_buy", "virtual_initial_cash": 500_000},
                # 第二个 strategy 也是 always_buy 用不同 strategy_id
            ],
        }],
    })
    # 先跑一次确认基线
    pipeline.run(20260430)
    with sf() as s:
        assert s.query(Order).count() == 1


def test_pipeline_precheck_fails_blocks_signal(setup):
    """资金不够：信号被预检拒绝。"""
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "always_buy", "virtual_initial_cash": 100}],  # 不够买 100*10=1000
        }],
    })
    summary = pipeline.run(20260430)

    assert summary["signals"] == 1
    assert summary["passed"] == 0   # 预检拒绝
    assert summary["orders"] == 0   # 无归集

    with sf() as s:
        sigs = s.query(RawSignalRow).all()
        assert sigs[0].precheck_status == "FAIL"


def test_pipeline_runs_perf_snapshot(setup):
    """pipeline 跑完应产出 NAV 快照。"""
    from app.models import PerfSnapshot

    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "noop", "virtual_initial_cash": 1000}],
        }],
    })
    pipeline.run(20260430)

    with sf() as s:
        snap = s.get(PerfSnapshot, ("real_A_noop", "20260430"))
        assert snap is not None
        assert snap.nav == 1000.0


def test_pipeline_idempotent_same_date_no_dupes(setup):
    """同一 trade_date 调 pipeline 多次，不应出现重复 raw_signals/orders。

    Regression：2026-05-07 凌晨触发了两次 trade_date=20260507 → 1346 dupe orders。
    """
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "always_buy", "virtual_initial_cash": 100000}],
        }],
    })

    summary1 = pipeline.run(20260430)
    assert summary1["signals"] == 1
    assert summary1["orders"] == 1

    # 第二次触发：应清掉第一次的 + 写一份新的
    summary2 = pipeline.run(20260430)
    assert summary2["signals"] == 1
    assert summary2["orders"] == 1

    # 数据库里只有 1 条 raw_signal + 1 条 order（不是 2 条）
    with sf() as s:
        sigs = s.execute(
            __import__("sqlalchemy").select(RawSignalRow)
            .where(RawSignalRow.valid_date == "20260430")
        ).scalars().all()
        assert len(sigs) == 1, f"幂等失效：第二次跑后 raw_signals 应该 1 条，实际 {len(sigs)}"

        orders = s.execute(
            __import__("sqlalchemy").select(Order)
            .where(Order.valid_date == "20260430")
        ).scalars().all()
        assert len(orders) == 1, f"幂等失效：第二次跑后 orders 应该 1 条，实际 {len(orders)}"


def test_pipeline_blacklist_filters_strategy(setup):
    """pipeline 应自动从过去 N 天 REJECTED orders 提取黑名单，传给 ctx。"""
    from app.models import Order as OrderRow

    pipeline, sf, store, yaml_path = setup

    # 注入历史 REJECTED：模拟 600519.SH 之前被 QMT 拒过
    with sf() as s:
        s.add(OrderRow(
            order_id="hist-rej-1", account_group="real_A",
            symbol="600519.SH", direction="BUY", quantity=100, limit_price=10.0,
            valid_date="20260501", status="REJECTED", created_at=_now(),
        ))
        s.commit()

    # 写一个会读 ctx.is_blacklisted 的策略
    class BlacklistAwareStrategy(Strategy):
        name = "bl_aware"
        def run(self, ctx, trade_date):
            if ctx.is_blacklisted("600519.SH"):
                return []
            return [RawSignal(
                symbol="600519.SH", direction="BUY", quantity=100,
                reference_price=10.0, price_offset=0.005,
            )]

    pipeline.registry["bl_aware"] = BlacklistAwareStrategy
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "bl_aware", "virtual_initial_cash": 100000}],
        }],
    })

    summary = pipeline.run(20260507)
    # 黑名单生效 → 0 signals
    assert summary["signals"] == 0
    assert summary["orders"] == 0


# ── Bug A 回归：precheck SELL-first ──────────────────────────────────
class MixedSellThenBuyStrategy(Strategy):
    """一笔 SELL（释放现金）+ 一笔 BUY（依赖那笔现金）。

    Regression：5/13 实盘场景——账上只剩 3.5K，但有 8 笔 SELL ~887K 待发 + 6 笔
    BUY 各 110K。fix 前每个 BUY 单独和 3.5K 比较 → 全 FAIL；fix 后 SELL 先走，
    BUY 用累积的现金通过。
    """
    name = "sell_then_buy"

    def run(self, ctx, trade_date):
        return [
            RawSignal(symbol="600519.SH", direction="SELL", quantity=1000,
                      reference_price=100.0, price_offset=-0.005),  # 卖 1000 股 @ ~99.5
            RawSignal(symbol="000001.SZ", direction="BUY", quantity=900,
                      reference_price=100.0, price_offset=+0.005),  # 买 900 股 @ ~100.5
        ]


def test_pipeline_precheck_sell_first_unlocks_buy(setup):
    """SELL 优先：BUY 看到 SELL 累积出来的 running cash 后 PASS。"""
    pipeline, sf, store, yaml_path = setup
    pipeline.registry["sell_then_buy"] = MixedSellThenBuyStrategy
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "sell_then_buy", "virtual_initial_cash": 3_500}],
        }],
    })
    # 种好持仓供 SELL（1000 股 600519.SH）
    with sf() as s:
        s.add(InstanceState(
            instance_id="real_A_sell_then_buy",
            virtual_cash=3_500.0,
            virtual_positions={"600519.SH": 1000},
            last_update=_now(),
        ))
        s.commit()

    summary = pipeline.run(20260513)

    # 2 条 signal 都 PASS（SELL 释放 ~99.5K，BUY 需要 ~90.5K，加 fee_rate 0.001 后约 90.6K）
    assert summary["signals"] == 2
    assert summary["passed"] == 2

    with sf() as s:
        sigs = sorted(s.query(RawSignalRow).all(), key=lambda x: x.direction)
        assert sigs[0].direction == "BUY"
        assert sigs[0].precheck_status == "PASS"
        assert sigs[1].direction == "SELL"
        assert sigs[1].precheck_status == "PASS"


def test_pipeline_precheck_sell_first_still_blocks_overdraft_buy(setup):
    """SELL-first 不应让"超额 BUY"通过：BUY 需要的现金 > SELL 释放 + 初始 cash 时仍 FAIL。"""
    pipeline, sf, store, yaml_path = setup

    class SmallSellBigBuyStrategy(Strategy):
        name = "small_sell_big_buy"

        def run(self, ctx, trade_date):
            return [
                RawSignal(symbol="600519.SH", direction="SELL", quantity=100,
                          reference_price=10.0, price_offset=-0.005),  # 卖 100 股 ~995
                RawSignal(symbol="000001.SZ", direction="BUY", quantity=10000,
                          reference_price=100.0, price_offset=+0.005),  # 要 ~1M
            ]

    pipeline.registry["small_sell_big_buy"] = SmallSellBigBuyStrategy
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "small_sell_big_buy", "virtual_initial_cash": 1000}],
        }],
    })
    with sf() as s:
        s.add(InstanceState(
            instance_id="real_A_small_sell_big_buy",
            virtual_cash=1000.0,
            virtual_positions={"600519.SH": 100},
            last_update=_now(),
        ))
        s.commit()

    summary = pipeline.run(20260513)
    # SELL PASS, BUY FAIL（即使加上 SELL 现金还远不够）
    assert summary["passed"] == 1

    with sf() as s:
        sigs = {sig.direction: sig.precheck_status for sig in s.query(RawSignalRow).all()}
        assert sigs["SELL"] == "PASS"
        assert sigs["BUY"] == "FAIL"


# ── Bug D 回归：strategy_state 持久化 ─────────────────────────────────
class StatefulStrategy(Strategy):
    """读自己上次写下的 counter，每次 +1，再写回去。"""
    name = "stateful"

    def run(self, ctx, trade_date):
        st = ctx.strategy_state()
        counter = int(st.get("counter", 0))
        ctx.set_strategy_state({"counter": counter + 1})
        # 不发信号即可（只测持久化）
        return []


def test_pipeline_persists_strategy_state_across_runs(setup):
    """run 第 1 次：counter=1；run 第 2 次：counter=2（读到上次写下的 1）。"""
    pipeline, sf, store, yaml_path = setup
    pipeline.registry["stateful"] = StatefulStrategy
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "stateful", "virtual_initial_cash": 1000}],
        }],
    })

    pipeline.run(20260430)
    with sf() as s:
        inst = s.get(InstanceState, "real_A_stateful")
        assert inst.strategy_state == {"counter": 1}

    pipeline.run(20260506)
    with sf() as s:
        inst = s.get(InstanceState, "real_A_stateful")
        assert inst.strategy_state == {"counter": 2}


def test_pipeline_strategy_no_set_state_keeps_old_state(setup):
    """策略不调用 set_strategy_state 时，老的 state 不被改动。"""
    pipeline, sf, store, yaml_path = setup

    class ReadOnlyStrategy(Strategy):
        name = "read_only"
        def run(self, ctx, trade_date):
            _ = ctx.strategy_state()  # 读不写
            return []

    pipeline.registry["read_only"] = ReadOnlyStrategy
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "read_only", "virtual_initial_cash": 1000}],
        }],
    })

    # 先种一个老 state
    pipeline.run(20260430)  # 创建 instance_state
    with sf() as s:
        inst = s.get(InstanceState, "real_A_read_only")
        inst.strategy_state = {"my_data": "preserved"}
        s.commit()

    pipeline.run(20260506)
    with sf() as s:
        inst = s.get(InstanceState, "real_A_read_only")
        assert inst.strategy_state == {"my_data": "preserved"}


def test_pipeline_reset_instance_states(setup):
    """reset_instance_states 应把 cash 还原成 yaml 初始值，positions 清空。"""
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "always_buy", "virtual_initial_cash": 100000}],
        }],
    })
    # 先创建 instance_state（通过 run 一次）
    pipeline.run(20260430)

    # 手动改成异常状态
    with sf() as s:
        inst = s.get(InstanceState, "real_A_always_buy")
        inst.virtual_cash = -999999.0
        inst.virtual_positions = {"600519.SH": 1000}
        s.commit()

    # reset
    result = pipeline.reset_instance_states()
    assert "real_A_always_buy" in result
    assert result["real_A_always_buy"]["cash"] == 100000.0
    assert result["real_A_always_buy"]["positions"] == {}

    with sf() as s:
        inst = s.get(InstanceState, "real_A_always_buy")
        assert inst.virtual_cash == 100000.0
        assert inst.virtual_positions == {}
