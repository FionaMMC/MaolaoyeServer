# Plan 06: 策略框架 + 插件加载器

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 让用户在 `plugins/` 下丢一个 .py 文件就能注册一个新策略；server 启动时自动扫描 + 注册；行情入库后调用 runner 跑所有策略实例并收集 RawSignal（先存内存，DB 落地放 Plan 12）。

**Architecture:**
- `Strategy` 抽象基类 + `RawSignal` dataclass：策略契约
- `Context` 类：注入给策略的运行时上下文（virtual cash + positions + 历史行情 + universe）
- `load_plugins(dir) -> dict[name, type[Strategy]]`：扫描 `*.py` 用 importlib，按 `Strategy.name` class attr 注册
- `StrategyRunner`：对每个 (account_group, strategy_id) 实例构造 Context 调 `.run()`
- 插件错误隔离：单个 plugin 加载失败 / 单个策略 run 抛异常 都不应 crash 其他

**Files:**
- `v2.3/server/app/strategy/__init__.py` (NEW, `# 包标记`)
- `v2.3/server/app/strategy/base.py` (NEW)
- `v2.3/server/app/strategy/context.py` (NEW)
- `v2.3/server/app/strategy/loader.py` (NEW)
- `v2.3/server/app/strategy/runner.py` (NEW)
- `v2.3/server/plugins/README.md` (NEW)
- `v2.3/server/plugins/_example_buy_threshold.py` (NEW)
- `v2.3/server/tests/unit/test_strategy_base.py` (NEW)
- `v2.3/server/tests/unit/test_strategy_context.py` (NEW)
- `v2.3/server/tests/unit/test_strategy_loader.py` (NEW)
- `v2.3/server/tests/unit/test_strategy_runner.py` (NEW)

---

## Task 1: Strategy ABC + RawSignal dataclass

### `app/strategy/__init__.py`

```python
# 包标记
```

### `app/strategy/base.py`

```python
"""策略框架契约：Strategy 抽象基类 + RawSignal 输出 dataclass。"""
from __future__ import annotations

import abc
from dataclasses import dataclass

from app.strategy.context import Context


@dataclass(frozen=True)
class RawSignal:
    """策略输出。归集前的原始信号。"""
    symbol: str
    direction: str               # BUY / SELL
    quantity: int                # 股数；BUY 必为 100 整数倍
    reference_price: float       # 参考价（通常是当日 close）
    price_offset: float          # 超价幅度，BUY 正 / SELL 负

    def __post_init__(self):
        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"direction must be BUY or SELL, got {self.direction!r}")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.reference_price <= 0:
            raise ValueError(f"reference_price must be positive, got {self.reference_price}")


class Strategy(abc.ABC):
    """所有策略插件必须继承此基类。

    子类必须:
      - 设置 class attr `name`（与 strategies.yaml 的 strategy_id 对齐）
      - 实现 `run(ctx, trade_date) -> list[RawSignal]`
    """

    name: str = ""   # 子类必须覆盖

    @abc.abstractmethod
    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        """根据 ctx 提供的虚拟账本和行情，输出当日原始信号列表。

        Args:
            ctx: 运行时上下文（资金、持仓、行情、universe）
            trade_date: 当前交易日 YYYYMMDD（int）

        Returns:
            原始信号列表。空列表表示策略当日决定不交易。
        """
        ...
```

### `tests/unit/test_strategy_base.py`

```python
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
        s.symbol = "B.SH"   # frozen dataclass


def test_strategy_is_abstract():
    """不能直接实例化 Strategy。"""
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
```

---

## Task 2: Context

### `app/strategy/context.py`

```python
"""Context：注入给策略的运行时上下文。"""
from __future__ import annotations

import pandas as pd

from app.storage.parquet import Category, ParquetStore


class Context:
    """策略运行时上下文。每次策略实例运算前由 Runner 构造。

    instance 标识 + 虚拟账本（资金/持仓）+ 行情读取 + universe。
    """

    def __init__(
        self,
        instance_id: str,
        trade_date: int,
        virtual_cash: float,
        virtual_positions: dict[str, int],
        parquet_store: ParquetStore,
    ):
        self.instance_id = instance_id
        self.trade_date = trade_date
        self._cash = float(virtual_cash)
        self._positions = dict(virtual_positions)
        self._store = parquet_store

    # ── 资金 / 持仓 ───────────────────────────────────────────────────
    def cash(self) -> float:
        """当前虚拟可用资金（元）。"""
        return self._cash

    def position(self, symbol: str) -> int:
        """单标的当前虚拟持仓股数。无持仓返回 0。"""
        return int(self._positions.get(symbol, 0))

    def positions(self) -> dict[str, int]:
        """所有标的的当前虚拟持仓快照（拷贝，修改不影响内部状态）。"""
        return dict(self._positions)

    # ── 行情 ──────────────────────────────────────────────────────────
    def market(
        self,
        symbol: str,
        *,
        start_date: int | None = None,
        end_date: int | None = None,
        fields: list[str] | None = None,
        category: Category = "stocks",
    ) -> pd.DataFrame:
        """读取标的历史 OHLCV。

        Args:
            symbol: 如 "600519.SH"
            start_date / end_date: 含端点的日期范围（YYYYMMDD int）；
                                  默认 end_date = ctx.trade_date
            fields: 只取指定字段；不传则全部
            category: stocks / etfs / indexes

        Returns:
            DataFrame，按 trade_date 升序。无数据返回空 DataFrame。
        """
        end = self.trade_date if end_date is None else end_date
        df = self._store.read(category, symbol, start_date=start_date, end_date=end)
        if fields and not df.empty:
            keep = ["trade_date"] + [f for f in fields if f in df.columns]
            df = df[keep]
        return df

    def universe(self, category: Category = "stocks") -> list[str]:
        """该 category 下当前所有有数据的 symbol 列表。"""
        return self._store.list_symbols(category)
```

### `tests/unit/test_strategy_context.py`

```python
"""Context 单元测试"""
from pathlib import Path

import pandas as pd

from app.storage.parquet import ParquetStore
from app.strategy.context import Context


def _row(d: int, close: float = 10.0) -> dict:
    return {"trade_date": d, "open": close, "high": close, "low": close,
            "close": close, "volume": 1000, "amount": close * 1000,
            "suspendFlag": 0}


def _ctx(tmp_path: Path, **overrides) -> Context:
    store = ParquetStore(root=tmp_path)
    defaults = dict(
        instance_id="real_A_momentum", trade_date=20260430,
        virtual_cash=1_000_000.0, virtual_positions={},
        parquet_store=store,
    )
    defaults.update(overrides)
    return Context(**defaults)


def test_context_cash(tmp_path: Path):
    ctx = _ctx(tmp_path, virtual_cash=500_000.0)
    assert ctx.cash() == 500_000.0


def test_context_position_existing(tmp_path: Path):
    ctx = _ctx(tmp_path, virtual_positions={"600519.SH": 200, "000001.SZ": 500})
    assert ctx.position("600519.SH") == 200
    assert ctx.position("000001.SZ") == 500


def test_context_position_nonexistent_returns_zero(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assert ctx.position("NOT_HELD.SH") == 0


def test_context_positions_returns_copy(tmp_path: Path):
    ctx = _ctx(tmp_path, virtual_positions={"A.SH": 100})
    snapshot = ctx.positions()
    snapshot["A.SH"] = 999   # 修改快照不影响 ctx 内部
    assert ctx.position("A.SH") == 100


def test_context_market_returns_history(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH",
                 pd.DataFrame([_row(20260427), _row(20260428), _row(20260430)]))
    ctx = Context(instance_id="x", trade_date=20260430,
                  virtual_cash=0.0, virtual_positions={},
                  parquet_store=store)
    df = ctx.market("600519.SH")
    assert df["trade_date"].tolist() == [20260427, 20260428, 20260430]


def test_context_market_default_end_is_trade_date(tmp_path: Path):
    """不传 end_date 时应默认到 ctx.trade_date，不包括未来数据。"""
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "A.SH",
                 pd.DataFrame([_row(20260428), _row(20260429), _row(20260430)]))
    ctx = Context(instance_id="x", trade_date=20260429,
                  virtual_cash=0.0, virtual_positions={},
                  parquet_store=store)
    df = ctx.market("A.SH")
    assert df["trade_date"].max() == 20260429   # 不包含 20260430


def test_context_market_with_fields_filter(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "A.SH", pd.DataFrame([_row(20260430)]))
    ctx = Context(instance_id="x", trade_date=20260430,
                  virtual_cash=0.0, virtual_positions={},
                  parquet_store=store)
    df = ctx.market("A.SH", fields=["close"])
    assert set(df.columns) == {"trade_date", "close"}


def test_context_market_nonexistent_symbol_empty(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assert ctx.market("NOT_EXIST.SH").empty


def test_context_universe(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "A.SH", pd.DataFrame([_row(20260430)]))
    store.append("stocks", "B.SH", pd.DataFrame([_row(20260430)]))
    store.append("indexes", "000300.SH", pd.DataFrame([_row(20260430)]))
    ctx = Context(instance_id="x", trade_date=20260430,
                  virtual_cash=0.0, virtual_positions={},
                  parquet_store=store)
    assert sorted(ctx.universe("stocks")) == ["A.SH", "B.SH"]
    assert ctx.universe("indexes") == ["000300.SH"]
    assert ctx.universe("etfs") == []
```

---

## Task 3: Plugin Loader

### `app/strategy/loader.py`

```python
"""扫描 plugins/ 目录加载策略类。

约定：
- plugins/*.py 中每个 Strategy 子类按 .name 注册
- 同名策略冲突 → 抛 ValueError
- 单个 plugin 加载错误 → 记日志，继续加载其他（不 crash 整个 loader）
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import Type

from app.strategy.base import Strategy

logger = logging.getLogger(__name__)


def load_plugins(plugins_dir: Path | str) -> dict[str, Type[Strategy]]:
    """扫描目录下所有 .py（含 _ 前缀的示例）但跳过 __init__.py。

    Returns: {strategy.name: Strategy 子类}
    Raises: ValueError 如果有同名冲突
    """
    plugins_dir = Path(plugins_dir)
    if not plugins_dir.exists():
        logger.warning("plugins_dir 不存在: %s", plugins_dir)
        return {}

    registry: dict[str, Type[Strategy]] = {}

    for py_file in sorted(plugins_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        try:
            module = _import_file(py_file)
        except Exception as e:  # noqa: BLE001
            logger.error("plugin 加载失败 %s: %s", py_file.name, e)
            continue

        for name, obj in inspect.getmembers(module, inspect.isclass):
            if obj is Strategy or not issubclass(obj, Strategy):
                continue
            if obj.__module__ != module.__name__:
                continue   # 来自 import 的，不重复注册
            if not obj.name:
                logger.error("strategy class %s 无 .name 属性", name)
                continue
            if obj.name in registry:
                raise ValueError(
                    f"strategy name 冲突: '{obj.name}' 已被 "
                    f"{registry[obj.name].__module__} 注册，"
                    f"无法再注册 {obj.__module__}"
                )
            registry[obj.name] = obj
            logger.info("注册策略 '%s' from %s", obj.name, py_file.name)

    return registry


def _import_file(path: Path):
    """通过 importlib 加载单个 .py 文件，避免污染包路径。"""
    mod_name = f"_qmt_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module
```

### `tests/unit/test_strategy_loader.py`

```python
"""plugin loader 测试"""
from pathlib import Path

import pytest

from app.strategy.base import Strategy
from app.strategy.loader import load_plugins


def _write(p: Path, content: str):
    p.write_text(content, encoding="utf-8")


def test_load_empty_dir(tmp_path: Path):
    assert load_plugins(tmp_path) == {}


def test_load_nonexistent_dir(tmp_path: Path):
    assert load_plugins(tmp_path / "nope") == {}


def test_load_single_plugin(tmp_path: Path):
    _write(tmp_path / "my_strat.py", '''
from app.strategy.base import Strategy

class MyStrat(Strategy):
    name = "my_strat"
    def run(self, ctx, trade_date):
        return []
''')
    registry = load_plugins(tmp_path)
    assert "my_strat" in registry
    assert issubclass(registry["my_strat"], Strategy)
    assert registry["my_strat"].name == "my_strat"


def test_load_skips_init_py(tmp_path: Path):
    _write(tmp_path / "__init__.py", "# nothing")
    _write(tmp_path / "good.py", '''
from app.strategy.base import Strategy

class S(Strategy):
    name = "good"
    def run(self, ctx, trade_date): return []
''')
    registry = load_plugins(tmp_path)
    assert list(registry.keys()) == ["good"]


def test_load_includes_underscore_prefixed_files(tmp_path: Path):
    """_example_xxx.py 也应被加载（不跳过）。"""
    _write(tmp_path / "_example.py", '''
from app.strategy.base import Strategy

class Ex(Strategy):
    name = "example"
    def run(self, ctx, trade_date): return []
''')
    registry = load_plugins(tmp_path)
    assert "example" in registry


def test_load_duplicate_name_raises(tmp_path: Path):
    _write(tmp_path / "a.py", '''
from app.strategy.base import Strategy
class A(Strategy):
    name = "dup"
    def run(self, ctx, trade_date): return []
''')
    _write(tmp_path / "b.py", '''
from app.strategy.base import Strategy
class B(Strategy):
    name = "dup"
    def run(self, ctx, trade_date): return []
''')
    with pytest.raises(ValueError, match="name 冲突"):
        load_plugins(tmp_path)


def test_load_plugin_with_import_error_skipped(tmp_path: Path, caplog):
    _write(tmp_path / "bad.py", "raise ImportError('boom')")
    _write(tmp_path / "good.py", '''
from app.strategy.base import Strategy
class G(Strategy):
    name = "good"
    def run(self, ctx, trade_date): return []
''')
    registry = load_plugins(tmp_path)
    assert "good" in registry
    assert "bad" not in registry  # 没法注册因为加载失败


def test_load_plugin_without_name_skipped(tmp_path: Path):
    _write(tmp_path / "noname.py", '''
from app.strategy.base import Strategy
class NoName(Strategy):
    # 故意不设 name
    def run(self, ctx, trade_date): return []
''')
    registry = load_plugins(tmp_path)
    assert registry == {}
```

---

## Task 4: Runner

### `app/strategy/runner.py`

```python
"""StrategyRunner：迭代所有策略实例，构造 Context，跑 strategy.run()。

错误隔离：单个 strategy.run() 抛异常时记日志、跳过，不影响其他实例。
DB 落地放 Plan 12（scheduler）。本 plan 只返回内存结果。
"""
from __future__ import annotations

import logging
from typing import Type

from app.storage.parquet import ParquetStore
from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context

logger = logging.getLogger(__name__)


class StrategyRunner:
    """根据 strategies.yaml 配置，对每个 (account_group, strategy_id) 跑策略。"""

    def __init__(
        self,
        registry: dict[str, Type[Strategy]],
        parquet_store: ParquetStore,
    ):
        self.registry = registry
        self.store = parquet_store

    def run_all(
        self,
        trade_date: int,
        instances: list[dict],
    ) -> dict[str, list[RawSignal]]:
        """执行所有实例的策略，返回 {instance_id: [RawSignal...]}.

        Args:
            trade_date: 当日 YYYYMMDD（int）
            instances: 每条形如:
                {
                  "instance_id": "real_A_momentum",
                  "strategy_id": "momentum",
                  "virtual_cash": 500000.0,
                  "virtual_positions": {"600519.SH": 100},
                }

        Returns:
            dict[instance_id, list[RawSignal]]. 失败的实例返回空 list 并记 error。
        """
        results: dict[str, list[RawSignal]] = {}

        for inst in instances:
            instance_id = inst["instance_id"]
            strategy_id = inst["strategy_id"]

            strategy_cls = self.registry.get(strategy_id)
            if strategy_cls is None:
                logger.error(
                    "instance %s: strategy '%s' 未注册（plugins/ 下无对应类）",
                    instance_id, strategy_id,
                )
                results[instance_id] = []
                continue

            ctx = Context(
                instance_id=instance_id,
                trade_date=trade_date,
                virtual_cash=inst["virtual_cash"],
                virtual_positions=inst.get("virtual_positions", {}),
                parquet_store=self.store,
            )

            try:
                signals = strategy_cls().run(ctx, trade_date)
                if not isinstance(signals, list):
                    raise TypeError(
                        f"strategy.run 必须返回 list[RawSignal]，实际 {type(signals)}"
                    )
                results[instance_id] = signals
            except Exception as e:  # noqa: BLE001
                logger.exception("instance %s strategy.run 抛异常: %s", instance_id, e)
                results[instance_id] = []

        return results
```

### `tests/unit/test_strategy_runner.py`

```python
"""StrategyRunner 测试"""
from pathlib import Path

import pandas as pd

from app.storage.parquet import ParquetStore
from app.strategy.base import RawSignal, Strategy
from app.strategy.runner import StrategyRunner


class FixedBuyStrategy(Strategy):
    """测试用：每次返回买茅台 100 股的固定信号。"""
    name = "fixed_buy"
    def run(self, ctx, trade_date):
        return [RawSignal(symbol="600519.SH", direction="BUY",
                          quantity=100, reference_price=10.0, price_offset=0.005)]


class EmptyStrategy(Strategy):
    name = "empty"
    def run(self, ctx, trade_date):
        return []


class CrashingStrategy(Strategy):
    name = "crash"
    def run(self, ctx, trade_date):
        raise RuntimeError("boom from strategy")


class BadReturnStrategy(Strategy):
    name = "bad_return"
    def run(self, ctx, trade_date):
        return "not a list"   # type: ignore


def _runner(tmp_path: Path) -> StrategyRunner:
    return StrategyRunner(
        registry={
            "fixed_buy": FixedBuyStrategy,
            "empty": EmptyStrategy,
            "crash": CrashingStrategy,
            "bad_return": BadReturnStrategy,
        },
        parquet_store=ParquetStore(root=tmp_path),
    )


def test_runner_runs_single_instance(tmp_path: Path):
    runner = _runner(tmp_path)
    out = runner.run_all(20260430, instances=[{
        "instance_id": "real_A_fixed_buy",
        "strategy_id": "fixed_buy",
        "virtual_cash": 1_000_000.0,
        "virtual_positions": {},
    }])
    assert "real_A_fixed_buy" in out
    sigs = out["real_A_fixed_buy"]
    assert len(sigs) == 1
    assert sigs[0].symbol == "600519.SH"


def test_runner_iterates_multiple_instances(tmp_path: Path):
    runner = _runner(tmp_path)
    out = runner.run_all(20260430, instances=[
        {"instance_id": "i1", "strategy_id": "fixed_buy",
         "virtual_cash": 100.0, "virtual_positions": {}},
        {"instance_id": "i2", "strategy_id": "empty",
         "virtual_cash": 100.0, "virtual_positions": {}},
    ])
    assert len(out["i1"]) == 1
    assert out["i2"] == []


def test_runner_unknown_strategy_id_logs_and_returns_empty(tmp_path: Path, caplog):
    runner = _runner(tmp_path)
    out = runner.run_all(20260430, instances=[
        {"instance_id": "i1", "strategy_id": "nonexistent",
         "virtual_cash": 0.0, "virtual_positions": {}},
    ])
    assert out["i1"] == []


def test_runner_crashing_strategy_isolated(tmp_path: Path):
    """单实例策略抛异常不影响其他实例。"""
    runner = _runner(tmp_path)
    out = runner.run_all(20260430, instances=[
        {"instance_id": "crash_i", "strategy_id": "crash",
         "virtual_cash": 0.0, "virtual_positions": {}},
        {"instance_id": "good_i", "strategy_id": "fixed_buy",
         "virtual_cash": 0.0, "virtual_positions": {}},
    ])
    assert out["crash_i"] == []        # crash 实例返回空
    assert len(out["good_i"]) == 1     # 其他实例正常


def test_runner_bad_return_type_isolated(tmp_path: Path):
    runner = _runner(tmp_path)
    out = runner.run_all(20260430, instances=[
        {"instance_id": "i1", "strategy_id": "bad_return",
         "virtual_cash": 0.0, "virtual_positions": {}},
    ])
    assert out["i1"] == []
```

---

## Task 5: 例子插件 + README

### `plugins/_example_buy_threshold.py`

```python
"""示例策略：当某只股票当日 close 跌破最近 5 日均线 5% 时买入 100 股。

仅作框架演示，**生产策略请单独写**。
"""
from __future__ import annotations

from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context


class BuyOnDipExample(Strategy):
    """买跌示例：close < 5日均线 × 0.95 → BUY 100 股。"""

    name = "buy_on_dip_example"

    LOOKBACK = 5
    DROP_THRESHOLD = 0.95
    LIMIT_OFFSET = 0.005

    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        signals: list[RawSignal] = []
        for symbol in ctx.universe("stocks"):
            df = ctx.market(symbol, fields=["close"])
            if len(df) < self.LOOKBACK:
                continue
            recent = df.tail(self.LOOKBACK)
            today_close = float(recent["close"].iloc[-1])
            ma = float(recent["close"].mean())
            if today_close < ma * self.DROP_THRESHOLD and ctx.position(symbol) == 0:
                # 资金估算：能不能买 100 股？
                cost = today_close * 100 * (1 + self.LIMIT_OFFSET)
                if cost <= ctx.cash():
                    signals.append(RawSignal(
                        symbol=symbol, direction="BUY", quantity=100,
                        reference_price=today_close,
                        price_offset=self.LIMIT_OFFSET,
                    ))
        return signals
```

### `plugins/README.md`

```markdown
# 策略插件目录

把你的策略 .py 文件丢到这个目录，server 启动时会自动扫描注册。

## 契约

每个文件定义一个或多个继承 `app.strategy.base.Strategy` 的子类：

```python
from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context


class MyStrategy(Strategy):
    name = "my_strategy"   # 必填，须与 strategies.yaml 的 strategy_id 对齐

    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        # ctx.cash() / ctx.position(symbol) / ctx.positions()
        # ctx.market(symbol, start_date=, end_date=, fields=, category=)
        # ctx.universe(category="stocks")
        return [RawSignal(symbol="600519.SH", direction="BUY",
                          quantity=100, reference_price=1500.0,
                          price_offset=0.005)]
```

## 注意事项

- `name` 必须唯一（跨所有插件文件）；冲突时 server 启动报错
- 单个 .py 加载失败时被跳过、记日志，不影响其他插件
- 单个策略 `run()` 抛异常时被捕获、记日志，该实例当日 0 信号，但其他实例不受影响
- `_` 开头的文件也会被加载（如 `_example_buy_threshold.py`）；约定 `_` 表示示例/未启用
- 不要在策略里直接读写 DB 或调网络 — Context API 是唯一推荐入口
```

---

## 验证 + Commit

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest -v   # 期望 50 prior + 7 base + 9 context + 7 loader + 5 runner = 78
```

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/app/strategy/ \
        v2.3/server/plugins/README.md v2.3/server/plugins/_example_buy_threshold.py \
        v2.3/server/tests/unit/test_strategy_base.py \
        v2.3/server/tests/unit/test_strategy_context.py \
        v2.3/server/tests/unit/test_strategy_loader.py \
        v2.3/server/tests/unit/test_strategy_runner.py
git commit -m "feat(server): add Strategy framework + Context + plugin loader + runner (Plan 06)"
```

---

## 收尾

- [ ] `pytest -v` 78 PASS
- [ ] 1 commit（feature 较大，整块提交便于回滚）

---

## 后续 plan

Plan 07: precheck（虚拟资金/持仓预检）
