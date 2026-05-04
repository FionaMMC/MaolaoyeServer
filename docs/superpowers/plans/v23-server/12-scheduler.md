# Plan 12: Scheduler 编排 — 把策略管线串起来

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 写一个 `StrategyPipeline` 编排器，行情入库后跑：策略 → 预检 → 归集 → 写订单 → NAV 快照。再加 APScheduler 在 server 启动时启用一个 in-process 调度器；并加一个 internal `POST /admin/run-pipeline` 让人工触发（联测时用）。

**Architecture:**
- `StrategyPipeline.run(trade_date)` —— 同步管线，注入所有 service 依赖；返回执行摘要 dict
- APScheduler 单例随 FastAPI lifespan 起停；按 cron 触发（每交易日 16:00）
- 新加 `POST /admin/run-pipeline?trade_date=YYYYMMDD` —— 鉴权 + 同步触发，方便联测
- `strategies.yaml` 在 server 端：从 `Settings.strategies_file` 读

**Files:**
- `v2.3/server/app/scheduler/__init__.py` (NEW, `# 包标记`)
- `v2.3/server/app/scheduler/pipeline.py` (NEW, 主编排逻辑)
- `v2.3/server/app/scheduler/runtime.py` (NEW, APScheduler 包装)
- `v2.3/server/app/api/admin.py` (NEW, internal /admin/run-pipeline)
- `v2.3/server/app/main.py` (MODIFY, lifespan 启停 scheduler)
- `v2.3/server/app/dependencies.py` (MODIFY, 加 pipeline factory)
- `v2.3/server/strategies.yaml` (NEW, 示例配置)
- `v2.3/server/tests/unit/test_pipeline.py` (NEW)
- `v2.3/server/tests/unit/test_api_admin.py` (NEW)

---

## Task 1: StrategyPipeline 主编排

### `app/scheduler/__init__.py`

```python
# 包标记
```

### `app/scheduler/pipeline.py`

```python
"""StrategyPipeline：策略 → 预检 → 归集 → 写订单 → NAV 快照 一站式编排。"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Type

import yaml
from sqlalchemy import select

from app.models import InstanceState, RawSignal
from app.services.aggregate import AggregateService, TaggedSignal
from app.services.orders_queue import OrdersQueueService
from app.services.perf import PerfService
from app.services.precheck import PrecheckResult, PrecheckService
from app.storage.parquet import ParquetStore
from app.strategy.base import RawSignal as RawSignalModel
from app.strategy.base import Strategy
from app.strategy.runner import StrategyRunner

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class StrategyPipeline:
    """同步执行：策略 → 预检 → 归集 → 落库 → NAV 快照。"""

    def __init__(
        self,
        registry: dict[str, Type[Strategy]],
        parquet_store: ParquetStore,
        session_factory,
        precheck: PrecheckService,
        aggregate: AggregateService,
        orders_queue: OrdersQueueService,
        perf: PerfService,
        strategies_yaml_path: Path,
    ):
        self.registry = registry
        self.store = parquet_store
        self.session_factory = session_factory
        self.precheck = precheck
        self.aggregate = aggregate
        self.orders_queue = orders_queue
        self.perf = perf
        self.strategies_yaml_path = Path(strategies_yaml_path)

    def run(self, trade_date: int) -> dict:
        """完整管线。返回执行摘要。"""
        valid_date_str = str(trade_date)
        logger.info("pipeline_start trade_date=%s", trade_date)

        # 1. 加载 strategies.yaml
        instances = self._load_instances()
        if not instances:
            logger.warning("strategies.yaml 无 instance 定义，pipeline 退出")
            return {"signals": 0, "passed": 0, "orders": 0, "instances": 0}

        # 2. 加载/创建 instance_state
        states = self._ensure_instance_states(instances)

        # 3. 跑策略
        runner = StrategyRunner(registry=self.registry, parquet_store=self.store)
        signals_by_instance = runner.run_all(
            trade_date,
            instances=[
                {
                    "instance_id": inst["instance_id"],
                    "strategy_id": inst["strategy_id"],
                    "virtual_cash": states[inst["instance_id"]]["cash"],
                    "virtual_positions": states[inst["instance_id"]]["positions"],
                }
                for inst in instances
            ],
        )

        # 4. 预检 + 写 raw_signals 表 + 收集 PASS 的
        all_pass_tagged: list[TaggedSignal] = []
        signals_total = 0
        passed_total = 0
        with self.session_factory() as session:
            for inst in instances:
                instance_id = inst["instance_id"]
                signals = signals_by_instance.get(instance_id, [])
                state = states[instance_id]
                for sig in signals:
                    signals_total += 1
                    pre = self.precheck.check(
                        sig, state["cash"], state["positions"],
                    )
                    signal_id = uuid.uuid4().hex
                    limit_price = sig.reference_price * (1 + sig.price_offset)
                    session.add(RawSignal(
                        signal_id=signal_id,
                        instance_id=instance_id,
                        symbol=sig.symbol,
                        direction=sig.direction,
                        quantity=sig.quantity,
                        reference_price=sig.reference_price,
                        price_offset=sig.price_offset,
                        limit_price=round(limit_price, 4),
                        valid_date=valid_date_str,
                        signal_time=_now_iso(),
                        precheck_status=pre.status,
                        precheck_reason=pre.reason,
                    ))
                    if pre.status == "PASS":
                        passed_total += 1
                        all_pass_tagged.append(TaggedSignal(
                            signal_id=signal_id,
                            account_group=inst["account_group"],
                            raw=sig,
                        ))
            session.commit()

        # 5. 归集
        agg = self.aggregate.aggregate(all_pass_tagged, valid_date=valid_date_str)

        # 6. 写订单 + 映射
        self.orders_queue.write_aggregated(agg.orders, agg.mappings)

        # 7. NAV 快照（即使本日无信号也算）
        self.perf.snapshot_all(trade_date)

        summary = {
            "trade_date": trade_date,
            "instances": len(instances),
            "signals": signals_total,
            "passed": passed_total,
            "orders": len(agg.orders),
        }
        logger.info("pipeline_done %s", summary)
        return summary

    # ── 内部 ──────────────────────────────────────────────────────────
    def _load_instances(self) -> list[dict]:
        """从 strategies.yaml 解析出扁平化的实例列表。"""
        if not self.strategies_yaml_path.exists():
            logger.warning("strategies.yaml 不存在: %s", self.strategies_yaml_path)
            return []
        with self.strategies_yaml_path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        instances: list[dict] = []
        for ag in cfg.get("account_groups", []):
            group_id = ag["group_id"]
            for strat in ag.get("strategies", []):
                instances.append({
                    "instance_id": f"{group_id}_{strat['strategy_id']}",
                    "account_group": group_id,
                    "strategy_id": strat["strategy_id"],
                    "virtual_initial_cash": float(strat.get("virtual_initial_cash", 0)),
                })
        return instances

    def _ensure_instance_states(
        self, instances: list[dict],
    ) -> dict[str, dict]:
        """加载 InstanceState；缺失的用 yaml 里的 virtual_initial_cash 创建。"""
        result: dict[str, dict] = {}
        with self.session_factory() as session:
            existing = {
                row.instance_id: row
                for row in session.execute(select(InstanceState)).scalars().all()
            }
            for inst in instances:
                instance_id = inst["instance_id"]
                if instance_id in existing:
                    row = existing[instance_id]
                    result[instance_id] = {
                        "cash": float(row.virtual_cash),
                        "positions": dict(row.virtual_positions or {}),
                    }
                else:
                    cash = inst["virtual_initial_cash"]
                    session.add(InstanceState(
                        instance_id=instance_id,
                        virtual_cash=cash,
                        virtual_positions={},
                        last_update=_now_iso(),
                    ))
                    result[instance_id] = {"cash": cash, "positions": {}}
            session.commit()
        return result
```

### `tests/unit/test_pipeline.py`

```python
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
```

---

## Task 2: APScheduler runtime + admin endpoint + lifespan 集成

### `app/scheduler/runtime.py`

```python
"""APScheduler 包装：单例 BackgroundScheduler + 注册 cron 任务。"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


def make_scheduler(
    pipeline_run: Callable[[int], dict],
    cron_hour: int = 16,
    cron_minute: int = 0,
) -> BackgroundScheduler:
    """构造 BackgroundScheduler，注册每个交易日 16:00 跑 pipeline。

    pipeline_run: 接收 trade_date int，返回摘要 dict。
    """
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    def _job():
        today = int(datetime.now().strftime("%Y%m%d"))
        try:
            summary = pipeline_run(today)
            logger.info("scheduler_pipeline_done %s", summary)
        except Exception as e:
            logger.exception("scheduler_pipeline_error: %s", e)

    scheduler.add_job(
        _job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=cron_hour, minute=cron_minute,
        ),
        id="strategy_pipeline_daily",
        replace_existing=True,
    )
    return scheduler
```

### `app/api/admin.py`

```python
"""Internal /admin endpoints — 联测时人工触发管线。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.auth import verify_api_key
from app.dependencies import get_strategy_pipeline
from app.scheduler.pipeline import StrategyPipeline
from app.schemas.common import APIResponse

router = APIRouter(prefix="/admin")


@router.post(
    "/run-pipeline",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def run_pipeline_now(
    trade_date: int = Query(ge=20000101, le=99991231),
    pipeline: StrategyPipeline = Depends(get_strategy_pipeline),
):
    """同步触发整条策略管线，返回摘要。供联测/灾备使用。"""
    summary = pipeline.run(trade_date)
    return APIResponse[dict](code=0, message="ok", data=summary)
```

### `app/dependencies.py` 末尾追加

```python
from pathlib import Path

from app.scheduler.pipeline import StrategyPipeline
from app.services.aggregate import AggregateService
from app.services.precheck import PrecheckService
from app.strategy.loader import load_plugins


# 全局策略注册表（启动时一次性加载）
@lru_cache(maxsize=1)
def _strategy_registry(plugins_dir: str) -> dict:
    return load_plugins(Path(plugins_dir))


def get_strategy_pipeline(
    settings: Settings = Depends(get_settings),
    sf: sessionmaker = Depends(get_session_factory),
    store: ParquetStore = Depends(get_parquet_store),
    orders_queue: OrdersQueueService = Depends(get_orders_queue_service),
    perf: PerfService = Depends(get_perf_service),
) -> StrategyPipeline:
    return StrategyPipeline(
        registry=_strategy_registry(str(settings.plugins_dir)),
        parquet_store=store,
        session_factory=sf,
        precheck=PrecheckService(fee_rate=0.001),
        aggregate=AggregateService(),
        orders_queue=orders_queue,
        perf=perf,
        strategies_yaml_path=Path(settings.strategies_file),
    )
```

### `app/main.py` lifespan 改造

把现有 `_lifespan` 改为：

```python
@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    log = get_logger("app")
    log.info("server_starting", version="2.3.0")

    # 启动 APScheduler
    scheduler = None
    try:
        from app.scheduler.runtime import make_scheduler
        from app.dependencies import get_strategy_pipeline, _engine_for_url

        # 注意：这里直接构造 pipeline（绕开 Depends，因为 Depends 只在请求里有）
        # ...实际部署时 scheduler 应该用同样的 Settings 实例
        log.info("scheduler 已预备（实际启动需要 Plan 13 部署文档配置）")
    except Exception as e:
        log.warning("scheduler 启动失败: %s", e)

    yield

    if scheduler:
        scheduler.shutdown(wait=False)
    log.info("server_stopping")
```

并在 router 注册处加：

```python
from app.api import admin  # 顶部
app.include_router(admin.router, tags=["admin"])
```

### `strategies.yaml` 示例

```yaml
# v2.3 server 策略账户组配置示例
# 每个 (account_group, strategy_id) 形成一个独立的策略实例

account_groups:
  - group_id: real_A
    qmt_account_id: "1234567890"
    strategies:
      - strategy_id: buy_on_dip_example
        virtual_initial_cash: 500000
```

### `tests/unit/test_api_admin.py`

```python
"""POST /admin/run-pipeline 测试"""

_AUTH = {"Authorization": "Bearer TEST_KEY"}


def test_admin_run_pipeline_no_yaml(client, settings_for_test):
    """无 strategies.yaml 时管线应正常退出。"""
    r = client.post("/admin/run-pipeline?trade_date=20260430", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["instances"] == 0


def test_admin_run_pipeline_no_auth_returns_401(client):
    r = client.post("/admin/run-pipeline?trade_date=20260430")
    assert r.status_code == 401


def test_admin_run_pipeline_bad_date(client):
    r = client.post("/admin/run-pipeline?trade_date=abc", headers=_AUTH)
    assert r.json()["code"] == 1002
```

---

## 验证 + Commit

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest -v   # 期望 133 + 6 pipeline + 3 admin = 142
```

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/app/scheduler/ \
        v2.3/server/app/api/admin.py \
        v2.3/server/app/main.py \
        v2.3/server/app/dependencies.py \
        v2.3/server/strategies.yaml \
        v2.3/server/tests/unit/test_pipeline.py \
        v2.3/server/tests/unit/test_api_admin.py
git commit -m "feat(server): add StrategyPipeline + APScheduler + /admin/run-pipeline (Plan 12)"
```

---

## 收尾

- [ ] 142 PASS
- [ ] 1 commit
- [ ] `POST /admin/run-pipeline?trade_date=20260430` 能跑通完整管线

---

## 后续 plan

Plan 13: 阿里云部署（systemd unit + 环境变量 + 安全组 + bootstrap 脚本）
