"""StrategyPipeline 集成测试（用真实 SQLite + 真实 Parquet + 内联策略）"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState, Order, OrderSignalMap, RawSignal as RawSignalRow, Trade
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

    # 注意：pipeline 用 today 作为 snapshot 日期 (不是 trade_date)
    # trade_date=20260430 是 future date，snapshot 应该写在 today
    import datetime as _dt
    today_str = _dt.datetime.now().strftime("%Y%m%d")
    with sf() as s:
        snap = s.get(PerfSnapshot, ("real_A_noop", today_str))
        assert snap is not None, f"snapshot 应该写在 today={today_str}，不是 trade_date=20260430"
        assert snap.nav == 1000.0


def test_pipeline_does_not_snapshot_future_batch_without_today_market(setup):
    """A future batch without today's EOD data must not write any snapshot."""
    from app.models import PerfSnapshot

    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "noop", "virtual_initial_cash": 1000}],
        }],
    })
    # 一个明显的 future date
    future_date = 21260101
    summary = pipeline.run(future_date)

    import datetime as _dt
    today_str = _dt.datetime.now().strftime("%Y%m%d")

    with sf() as s:
        assert summary["skipped"] == "market_data_not_ready_for_future_batch"
        future_snap = s.get(PerfSnapshot, ("real_A_noop", str(future_date)))
        assert future_snap is None, "不该提前为 future trade_date 写 snapshot"
        today_snap = s.get(PerfSnapshot, ("real_A_noop", today_str))
        assert today_snap is None, "行情未到时不应写入任何 NAV snapshot"


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


def test_pipeline_rerun_preserves_settled_orders_no_orphan(setup):
    """Regression（2026-06 孤儿成交事故）：已结算订单不可被重算删除。

    复现链路：order A 收到成交回报 → settle() 写 Trade(A)、置 A=FILLED。之后 pipeline
    重算同一 valid_date → 旧版 _clear_for_date 把 A 删了 → Trade(A) 成孤儿；同时
    aggregate() 生成新 uuid order B（PENDING），客户端永不针对 B 回报 → SELL 永久卡
    PENDING。修复后：该日已结算 → 整轮重算跳过，A 与其 trade 原样保留，不重生成 order。
    """
    from app.models import Trade
    from app.services.ops_monitor import OpsMonitorService

    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "always_buy", "virtual_initial_cash": 100000}],
        }],
    })

    # 第一次跑：生成 order A
    pipeline.run(20260430)
    with sf() as s:
        orders = s.query(Order).all()
        assert len(orders) == 1
        order_a = orders[0].order_id

    # 模拟成交回报：写 trade + 标记 FILLED（等价 settle() 的落库效果）
    with sf() as s:
        s.add(Trade(order_id=order_a, filled_quantity=100, filled_price=10.0,
                    filled_time="2026-04-30T09:15:00", status="FILLED",
                    received_at=_now()))
        s.get(Order, order_a).status = "FILLED"
        s.commit()

    # 第二次跑同一 valid_date：必须保留已结算的 order A
    summary2 = pipeline.run(20260430)
    assert summary2.get("skipped") == "already_settled"

    with sf() as s:
        # order A 仍在（没被删）→ 其 trade 非孤儿
        assert s.get(Order, order_a) is not None, \
            "已结算的 order 被重算删除 → trade 成孤儿"
        # 没有为同一日重生成重复 order
        all_orders = s.query(Order).filter(Order.valid_date == "20260430").all()
        assert len(all_orders) == 1, \
            f"重算应跳过、不该重生成 order，实际 {len(all_orders)} 条"

    # orphan_fills 必须为 0（任务验收口径）
    orphans = OpsMonitorService(sf).orphan_fills(lookback_days=3650)
    assert orphans == [], f"重算后出现孤儿成交: {orphans}"


def test_clear_for_date_preserves_settled_orders(setup):
    """_clear_for_date（/admin/clear-state 的实现）只删未结算单，保留已结算单。

    保护手动 clear-state 路径：操作员清某日时不应连带删掉已收到成交回报的订单。
    """
    from app.models import Trade

    pipeline, sf, store, yaml_path = setup
    with sf() as s:
        s.add(Order(order_id="settled-1", account_group="real_A", symbol="600519.SH",
                    direction="SELL", quantity=100, limit_price=10.0,
                    valid_date="20260430", status="FILLED", created_at=_now()))
        s.add(Order(order_id="pending-1", account_group="real_A", symbol="000001.SZ",
                    direction="BUY", quantity=100, limit_price=10.0,
                    valid_date="20260430", status="PENDING", created_at=_now()))
        s.add(Trade(order_id="settled-1", filled_quantity=100, filled_price=10.0,
                    filled_time="2026-04-30T09:15:00", status="FILLED", received_at=_now()))
        s.commit()

    cleared = pipeline._clear_for_date("20260430")

    with sf() as s:
        assert s.get(Order, "settled-1") is not None, "已结算 order 不该被 clear 删除"
        assert s.get(Order, "pending-1") is None, "未结算 PENDING order 应被 clear 删除"
    assert cleared["orders"] == 1, "只应删除 pending-1 这一条"
    assert cleared.get("preserved_settled") == 1


def test_pipeline_blacklist_filters_strategy(setup):
    """pipeline 应自动从过去 N 天 REJECTED orders 提取黑名单，传给 ctx。"""
    from app.models import Order as OrderRow

    pipeline, sf, store, yaml_path = setup

    # 注入历史 REJECTED：模拟 600519.SH 之前被 QMT 拒过。
    # 日期必须相对今天（黑名单 cutoff = now - 30 天），写死月份会随时间滑出窗口。
    recent = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d")
    with sf() as s:
        s.add(OrderRow(
            order_id="hist-rej-1", account_group="real_A",
            symbol="600519.SH", direction="BUY", quantity=100, limit_price=10.0,
            valid_date=recent, status="REJECTED", created_at=_now(),
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


# ── Task 9: owned_symbols loader ──────────────────────────────────────
def test_pipeline_owned_symbols_none_legacy(setup):
    """yaml 没有 owned_symbols 键 → instance_state.owned_symbols 是 None。"""
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
        row = s.get(InstanceState, "real_A_noop")
        assert row is not None
        assert row.owned_symbols is None


def test_pipeline_owned_symbols_written_for_new_instance(setup):
    """yaml 有 owned_symbols 列表 → 新建的 instance_state.owned_symbols 与之一致。"""
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{
                "strategy_id": "noop",
                "virtual_initial_cash": 1000,
                "owned_symbols": ["600519.SH", "000001.SZ"],
            }],
        }],
    })
    pipeline.run(20260430)

    with sf() as s:
        row = s.get(InstanceState, "real_A_noop")
        assert row is not None
        assert row.owned_symbols == ["600519.SH", "000001.SZ"]


def test_pipeline_owned_symbols_updates_existing_instance(setup):
    """已存在的 instance_state（owned_symbols=None）在 yaml 增加后应被更新。"""
    pipeline, sf, store, yaml_path = setup

    # 先建实例，yaml 无 owned_symbols
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "noop", "virtual_initial_cash": 1000}],
        }],
    })
    pipeline.run(20260430)

    with sf() as s:
        row = s.get(InstanceState, "real_A_noop")
        assert row.owned_symbols is None

    # yaml 更新：加入 owned_symbols
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{
                "strategy_id": "noop",
                "virtual_initial_cash": 1000,
                "owned_symbols": ["300750.SZ"],
            }],
        }],
    })
    pipeline.run(20260501)

    with sf() as s:
        row = s.get(InstanceState, "real_A_noop")
        assert row.owned_symbols == ["300750.SZ"]


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


def test_live_pipeline_terminalizes_prior_day_unresolved_orders(setup, monkeypatch):
    import app.scheduler.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "_today_int", lambda: 20260728)
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "paper", "qmt_account_id": "X",
            "strategies": [{"strategy_id": "noop", "virtual_initial_cash": 1_000_000}],
        }],
    })
    with sf() as s:
        s.add(Order(
            order_id="old-pending", account_group="paper", symbol="600519.SH",
            direction="BUY", quantity=100, limit_price=10.0,
            valid_date="20260727", status="PENDING", created_at=_now(),
        ))
        s.add(Order(
            order_id="old-partial", account_group="paper", symbol="000001.SZ",
            direction="BUY", quantity=200, limit_price=10.0,
            valid_date="20260727", status="PARTIAL", created_at=_now(),
        ))
        s.add(Trade(
            order_id="old-partial", filled_quantity=100, filled_price=10.0,
            status="PARTIAL", filled_time=_now(), received_at=_now(),
        ))
        s.commit()

    summary = pipeline.run(20260728)

    assert summary["stale_orders_terminalized"] == {
        "expired": 1, "cancelled": 1, "skipped_with_trade": 0,
    }
    with sf() as s:
        assert s.get(Order, "old-pending").status == "EXPIRED"
        assert s.get(Order, "old-partial").status == "CANCELLED"


def test_future_batch_terminalizes_todays_partial_before_strict_guard(
    setup, monkeypatch,
):
    """EOD trigger for tomorrow must close today's PARTIAL remainder first.

    Regression: 2026-07-29 V7.13 had an EOD PARTIAL order.  The old cutoff used
    ``valid_date < today`` so the order remained unresolved and the strict guard
    blocked every strategy's 2026-07-30 batch and 2026-07-29 NAV snapshot.
    """
    import app.scheduler.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "_today_int", lambda: 20260729)
    pipeline, sf, store, yaml_path = setup
    monkeypatch.setattr(store, "latest_date", lambda *args, **kwargs: 20260729)
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "strict_dedicated", "qmt_account_id": "DEDICATED",
            "strategies": [{
                "strategy_id": "noop", "virtual_initial_cash": 1_000_000,
                "orders_enabled": True, "account_isolation": "dedicated",
                "requires_reconciled_rebalance": True,
            }],
        }],
    })
    with sf() as s:
        s.add(InstanceState(
            instance_id="strict_dedicated_noop", virtual_cash=1_000_000,
            virtual_positions={"510300.SH": 1100}, last_update=_now(),
        ))
        s.add(RawSignalRow(
            signal_id="today-partial-s1", instance_id="strict_dedicated_noop",
            symbol="510300.SH", direction="BUY", quantity=2200,
            reference_price=4.65, price_offset=0.0, limit_price=4.65,
            valid_date="20260729", signal_time=_now(), precheck_status="PASS",
        ))
        s.add(Order(
            order_id="today-partial-o1", account_group="strict_dedicated",
            symbol="510300.SH", direction="BUY", quantity=2200, limit_price=4.65,
            valid_date="20260729", status="PARTIAL", created_at=_now(),
        ))
        s.add(OrderSignalMap(
            order_id="today-partial-o1", signal_id="today-partial-s1",
            signal_quantity=2200,
        ))
        s.add(Trade(
            order_id="today-partial-o1", filled_quantity=1100, filled_price=4.65,
            status="PARTIAL", filled_time=_now(), received_at=_now(),
        ))
        s.commit()

    summary = pipeline.run(20260730)

    assert "skipped" not in summary
    assert summary["stale_orders_terminalized"] == {
        "expired": 0, "cancelled": 1, "skipped_with_trade": 0,
    }
    with sf() as s:
        assert s.get(Order, "today-partial-o1").status == "CANCELLED"
        trade = s.query(Trade).filter_by(order_id="today-partial-o1").one()
        assert trade.filled_quantity == 1100
        assert trade.status == "PARTIAL"
        state = s.get(InstanceState, "strict_dedicated_noop")
        assert state.virtual_cash == 1_000_000
        assert state.virtual_positions == {"510300.SH": 1100}


def test_strict_rebalance_guard_blocks_unresolved_and_unreconciled(setup):
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "strict_dedicated", "qmt_account_id": "DEDICATED",
            "strategies": [{
                "strategy_id": "noop", "virtual_initial_cash": 1_000_000,
                "orders_enabled": True, "account_isolation": "dedicated",
                "requires_reconciled_rebalance": True,
            }],
        }],
    })
    instances = pipeline._load_instances()
    pipeline._ensure_instance_states(instances)
    with sf() as s:
        inst = s.get(InstanceState, "strict_dedicated_noop")
        inst.strategy_state = {"reconciliation_status": "pending"}
        s.add(RawSignalRow(
            signal_id="strict-s1", instance_id=inst.instance_id,
            symbol="600519.SH", direction="BUY", quantity=100,
            reference_price=10.0, price_offset=0.0, limit_price=10.0,
            valid_date="20260720", signal_time=_now(), precheck_status="PASS",
        ))
        s.add(Order(
            order_id="strict-o1", account_group="strict_dedicated", symbol="600519.SH",
            direction="BUY", quantity=100, limit_price=10.0,
            valid_date="20260720", status="PARTIAL", created_at=_now(),
        ))
        s.add(OrderSignalMap(
            order_id="strict-o1", signal_id="strict-s1", signal_quantity=100,
        ))
        s.commit()

    guard = pipeline._execution_guards(instances)["strict_dedicated_noop"]
    assert not guard["allowed"]
    assert "unresolved_order" in guard["blockers"]
    assert "previous_rebalance_not_reconciled" in guard["blockers"]


def test_strict_rebalance_guard_requires_safe_account_boundary(setup):
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "strict_invalid", "qmt_account_id": None,
            "strategies": [{
                "strategy_id": "noop", "virtual_initial_cash": 1_000_000,
                "orders_enabled": False, "owned_symbols": [],
                "account_isolation": "none", "requires_reconciled_rebalance": True,
            }],
        }],
    })
    instances = pipeline._load_instances()
    pipeline._ensure_instance_states(instances)
    guard = pipeline._execution_guards(instances)["strict_invalid_noop"]
    assert set(guard["blockers"]) >= {
        "orders_disabled", "ambiguous_position_ownership", "missing_qmt_account",
    }


def test_dedicated_label_is_rejected_when_qmt_account_is_shared(setup):
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [
            {"group_id": "other", "qmt_account_id": "SAME", "strategies": [
                {"strategy_id": "noop", "virtual_initial_cash": 1_000_000},
            ]},
            {"group_id": "strict_dedicated", "qmt_account_id": "SAME", "strategies": [{
                "strategy_id": "always_buy", "virtual_initial_cash": 1_000_000,
                "orders_enabled": True, "account_isolation": "dedicated",
                "requires_reconciled_rebalance": True,
            }]},
        ],
    })
    instances = pipeline._load_instances()
    guard = pipeline._execution_guards(instances)["strict_dedicated_always_buy"]
    assert "qmt_account_not_dedicated" in guard["blockers"]


def test_shared_ledger_is_allowed_on_shared_qmt_account(setup):
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [
            {"group_id": "paper_v53", "qmt_account_id": "SAME", "strategies": [{
                "strategy_id": "noop", "virtual_initial_cash": 1_000_000,
                "owned_symbols": ["510300.SH"],
            }]},
            {"group_id": "paper_v79", "qmt_account_id": "SAME", "strategies": [{
                "strategy_id": "noop", "virtual_initial_cash": 1_000_000,
                "orders_enabled": True, "owned_symbols": [],
                "account_isolation": "shared_ledger",
                "requires_reconciled_rebalance": True,
            }]},
        ],
    })
    instances = pipeline._load_instances()
    guard = pipeline._execution_guards(instances)["paper_v79_noop"]
    assert guard["allowed"] is True
    assert guard["blockers"] == []
    assert guard["reconciliation_scope"] == "attributed_ledger"


def test_shared_ledger_can_claim_symbols_for_attributed_settlement(setup):
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "paper_v79", "qmt_account_id": "SAME",
            "strategies": [{
                "strategy_id": "noop", "virtual_initial_cash": 1_000_000,
                "orders_enabled": True, "owned_symbols": ["510300.SH"],
                "account_isolation": "shared_ledger",
                "requires_reconciled_rebalance": True,
            }],
        }],
    })
    instances = pipeline._load_instances()
    guard = pipeline._execution_guards(instances)["paper_v79_noop"]
    assert guard["allowed"] is True
    assert guard["blockers"] == []


def test_strict_preflight_preserves_unfetched_pending_order(setup):
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "strict_dedicated", "qmt_account_id": "DEDICATED",
            "strategies": [{
                "strategy_id": "noop", "virtual_initial_cash": 1_000_000,
                "orders_enabled": True, "account_isolation": "dedicated",
                "requires_reconciled_rebalance": True,
            }],
        }],
    })
    with sf() as s:
        s.add(InstanceState(
            instance_id="strict_dedicated_noop", virtual_cash=1_000_000,
            virtual_positions={}, last_update=_now(),
        ))
        s.add(RawSignalRow(
            signal_id="preserve-s1", instance_id="strict_dedicated_noop",
            symbol="600519.SH", direction="BUY", quantity=100,
            reference_price=10.0, price_offset=0.0, limit_price=10.0,
            valid_date="20260720", signal_time=_now(), precheck_status="PASS",
        ))
        s.add(Order(
            order_id="preserve-o1", account_group="strict_dedicated", symbol="600519.SH",
            direction="BUY", quantity=100, limit_price=10.0,
            valid_date="20260720", status="PENDING", created_at=_now(),
        ))
        s.add(OrderSignalMap(
            order_id="preserve-o1", signal_id="preserve-s1", signal_quantity=100,
        ))
        s.commit()

    summary = pipeline.run(20260720)
    assert summary["skipped"] == "strict_rebalance_blocked"
    with sf() as s:
        assert s.get(Order, "preserve-o1") is not None
        assert s.get(RawSignalRow, "preserve-s1") is not None


def test_pipeline_rerun_refuses_after_client_fetch(setup):
    """Regression（2026-07-02 成交未匹配事故）：客户端已拉取（fetched_at 非空）的
    valid_date 不允许默认重算——重算会换掉 order_id，客户端次日按旧 ID 回报成交
    → 服务器全量 unmatched，成交静默不入账。"""
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "always_buy", "virtual_initial_cash": 100000}],
        }],
    })

    pipeline.run(20260430)
    with sf() as s:
        orders = s.query(Order).all()
        assert len(orders) == 1
        order_a = orders[0].order_id
        # 模拟客户端 GET /orders 拉取
        s.get(Order, order_a).fetched_at = _now()
        s.commit()

    summary2 = pipeline.run(20260430)
    assert summary2.get("skipped") == "already_fetched"
    assert summary2.get("fetched_orders") == 1

    with sf() as s:
        # 订单未被换掉：仍是 order_a
        orders = s.query(Order).all()
        assert [o.order_id for o in orders] == [order_a], \
            "已拉取的订单被重算换掉 → 次日成交回报将全量 unmatched"


def test_pipeline_rerun_force_overrides_fetch_guard(setup):
    """force=True 显式重算已拉取批次：允许执行（操作者承诺让客户端重新拉取），
    摘要里带 force_regen_after_fetch 标记供审计。"""
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "always_buy", "virtual_initial_cash": 100000}],
        }],
    })

    pipeline.run(20260430)
    with sf() as s:
        order_a = s.query(Order).all()[0].order_id
        s.get(Order, order_a).fetched_at = _now()
        s.commit()

    summary2 = pipeline.run(20260430, force=True)
    assert summary2.get("skipped") is None
    assert summary2["orders"] == 1
    assert summary2.get("force_regen_after_fetch") == 1

    with sf() as s:
        orders = s.query(Order).all()
        assert len(orders) == 1
        assert orders[0].order_id != order_a  # 确实重生成了


def test_pipeline_settled_guard_not_bypassed_by_force(setup):
    """force 只放行 fetched 护栏；已结算护栏绝对不可绕过（删已结算单会孤儿化 trades）。"""
    from app.models import Trade

    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "always_buy", "virtual_initial_cash": 100000}],
        }],
    })

    pipeline.run(20260430)
    with sf() as s:
        order_a = s.query(Order).all()[0].order_id
        s.add(Trade(order_id=order_a, filled_quantity=100, filled_price=10.0,
                    filled_time="2026-04-30T09:15:00", status="FILLED",
                    received_at=_now()))
        s.get(Order, order_a).status = "FILLED"
        s.commit()

    summary2 = pipeline.run(20260430, force=True)
    assert summary2.get("skipped") == "already_settled"
    with sf() as s:
        assert s.get(Order, order_a) is not None
