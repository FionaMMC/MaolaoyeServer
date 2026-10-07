# Hydra 执行核心（第一阶段）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标**：在 10/7 前交付 Hydra 实盘执行核心。服务器端包括计划器、订单账本、对账、调仓周期、接口和危险入口改造；Windows 端包括执行代理和仿真交易所。验证分五层，重点是系统级回放：系统结果必须和研究回测逐轮一致。

**架构**：服务器新增 `app/oms/` 包，使用独立的 `oms_*` 表；成交通过现有 `SettlementService` 映射进策略账本，看板、净值、策略账本都不用改。Windows 端新增 `live_client/oms_agent.py`，复用 `XtQMTGateway`，本地日志单独放在 `oms-agent.db`，不再受"每个交易日只能有一个批次"的约束。计划器是纯函数，研究回测 `policy_replay.py` 直接导入它。

**技术栈**：Python 3.11、FastAPI、SQLAlchemy 2.0（`Mapped`/`mapped_column`，`create_all`）、SQLite、pytest、xtquant（只在 Windows 上使用）。

**设计依据**：`docs/superpowers/specs/2026-10-02-hydra-execution-core-design.md`。

**本期不做**：
- 紧急通道（用户已同意，之后单独设计）；
- 开盘前重算计划（由代理逐笔上限检查兜底）；
- 按持仓变化自动归因（十月改为暂停 + 人工脚本确认）；
- 模拟盘迁入、发布规范化。

**运行测试的方式**：在 `v2.3/server` 目录执行，只跑相关文件：

```bash
cd /d/server/v2.3/server && PYTHONPATH=.. python -m pytest tests/unit/test_oms_planner.py -q -o addopts=''
```

---

## 文件结构

**服务器（`v2.3/server`）**

| 文件 | 职责 |
|---|---|
| `app/oms/__init__.py` | 包声明 |
| `app/oms/planner.py` | 纯函数：整手目标、限价、资金分配、卖出和买入时段计划、时段日程 |
| `app/oms/states.py` | 订单状态机、QMT 状态码映射、旧系统状态映射 |
| `app/oms/models.py` | `oms_*` 七张表 |
| `app/oms/schemas.py` | 接口请求和响应的 pydantic 模型 |
| `app/oms/ledger.py` | 订单账本：建单（含旧系统镜像）、事件、券商观察、日终终结、映射进策略账本 |
| `app/oms/cycles.py` | 调仓周期：建立、批准、快照驱动（对账 → 终结 → 下一时段计划 → 关闭）、报告 |
| `app/oms/api.py` | `/oms/live/{snapshot,plan,events,status}` |
| `app/models/__init__.py` | 导入 OMS 模型，让 `create_all` 能建表 |
| `app/auth.py` | 实盘密钥白名单加入四个 `/oms/live/*` 路径 |
| `app/settings.py` | 新增 `oms_live_enabled: bool = False` |
| `app/main.py` | 注册 OMS 路由 |
| `app/api/live_trigger.py` | 不再执行实盘管线 |
| `app/scheduler/pipeline.py` | `run(..., execution_domain="live")` 一律拒绝 |
| `app/api/orders.py`、`app/api/trade_result.py`、`app/api/hydra_relay.py` | 开关打开后，实盘域的旧接口返回 409 |
| `scripts/oms_publish_target.py` | 月度权重 → 目标版本 + 待批准周期，并打印逐股清单 |
| `scripts/oms_approve_cycle.py` | 批准周期 |
| `scripts/oms_resolve.py` | 人工处理暂停：确认持仓变化、放行 |
| `scripts/oms_system_replay.py` | 在服务器上用真实研究数据跑 22 个周期的系统级回放 |
| `tests/unit/test_oms_*.py`、`tests/integration/test_oms_system_replay.py` | 测试 |

**Windows 客户端（`v2.3/live_client`）**

| 文件 | 职责 |
|---|---|
| `oms_journal.py` | 本地 SQLite：计划缓存、订单意图、待上传事件 |
| `gateway.py` | 新增 `submit_limit`、`day_orders`、`day_trades`、`quotes`；模拟网关同步补齐 |
| `sim_exchange.py` | 按 QMT 习性建模的仿真交易所和网关 |
| `http_client.py` | 新增 `post_oms_snapshot`、`get_oms_plan`、`post_oms_events` |
| `oms_agent.py` | 时段执行器：pre / sell / buy / cancel / eod，命令行入口 |
| `windows/Register-HydraOmsTasks.ps1` | 注册新任务，停用旧任务 |
| `oms_holiday_check.py` | 休市检查：只读快照，在模拟账户发一笔必然被拒的单 |
| `tests/test_oms_*.py` | 测试 |

**研究**：`reports/hydra_policy_20261002/policy_replay.py` 改为从 `app.oms.planner` 导入 `lot_target`、`buy_limit`、`sell_limit`、`allocate_buys`。

---

## 接口约定（所有任务共用，名字不得改）

```python
# app/oms/planner.py
LOT = 100; TICK = 0.001; POLICY_ID = "HYDRA_TWO_PHASE_C3_V1"
@dataclass(frozen=True) class Policy: policy_id, lot="nearest", size_factor=1.001, default_buy_bps=50.0,
    buy_bps=(("518880.SH",100.0),("513100.SH",150.0),("513500.SH",150.0)), sell_schedule=(50.0,200.0), window=3, reserve=0.0
    def buy_band(symbol) -> float; def sell_band(attempt: int) -> float
@dataclass(frozen=True) class PlannedOrder: symbol: str; side: str; quantity: int; limit_price: float; reference_price: float
@dataclass(frozen=True) class Deferral: symbol: str; side: str; quantity: int; reason: str   # NOT_SELLABLE|SUSPENDED|CASH
@dataclass(frozen=True) class Session: seq: int; trade_date: str; phase: str; sell_attempt: int | None
def lot_target(nav, weights, prices, policy) -> dict[str, int]
def buy_limit(anchor: float, bps: float) -> float
def sell_limit(anchor: float, bps: float) -> float
def allocate_buys(orders: list[dict], cash: float, lot_size: int, priorities: dict) -> list[dict]
def plan_sell_session(*, target, positions, sellable, sell_anchor, attempt, policy) -> tuple[list[PlannedOrder], list[Deferral]]
def plan_buy_session(*, target, positions, cash, buy_anchor, tradable, policy) -> tuple[list[PlannedOrder], list[Deferral]]
def session_schedule(calendar: Sequence[str], signal_date: str, window: int) -> list[Session]

# app/oms/states.py
class OrderState(str, Enum): PLANNED SUBMITTING ACKED PARTIAL PENDING_CANCEL UNKNOWN FILLED CANCELLED REJECTED EXPIRED_DAY NOT_SUBMITTED
TERMINAL: frozenset[OrderState]
def classify_qmt(status: int, traded: int, ordered: int) -> OrderState
def transition(current: OrderState, new: OrderState) -> OrderState      # 非法转移抛 IllegalTransition
def legacy_status(state: OrderState, filled: int) -> str | None          # 映射到旧 TradeResult.status；None 表示暂不映射

# 订单号：f"H{cycle_no:05d}{seq:02d}{leg:02d}"（10 个字符），同时用作 QMT 委托备注
```

快照、事件、计划的 JSON 结构以 `app/oms/schemas.py`（任务 5）为准，客户端照抄。

---

### 任务 1：计划器（纯函数）

**文件：**
- 新建：`v2.3/server/app/oms/__init__.py`（空文件）、`v2.3/server/app/oms/planner.py`
- 测试：`v2.3/server/tests/unit/test_oms_planner.py`

- [ ] **步骤 1：先写会失败的测试**

```python
from app.oms.planner import (Policy, Session, buy_limit, lot_target, plan_buy_session,
                             plan_sell_session, sell_limit, session_schedule)

P = Policy()

def test_limits_round_to_tick_away_from_chasing():
    assert buy_limit(2.196, 150) == 2.228
    assert buy_limit(9.105, 100) == 9.196
    assert sell_limit(4.621, 50) == 4.598
    assert sell_limit(4.621, 200) == 4.529

def test_nearest_lot_rounds_up_only_when_it_cuts_error_and_fits():
    prices = {"511260.SH": 136.0, "510300.SH": 4.6}
    t = lot_target(200000.0, {"511260.SH": 0.8, "510300.SH": 0.2}, prices, P)
    assert t["511260.SH"] % 100 == 0 and t["510300.SH"] % 100 == 0
    cost = sum(q * prices[s] for s, q in t.items()) * P.size_factor
    assert cost <= 200000.0
    assert t["511260.SH"] == 1100      # 160000/136/1.001 = 11.75 lots → 11; a 12th lot would overspend the basket
    assert t["510300.SH"] == 8700      # 86.87 lots → 86, +1 lot cuts the error (440 of 460) and still fits

def test_sell_session_caps_at_sellable_and_uses_attempt_band():
    orders, deferrals = plan_sell_session(
        target={"510300.SH": 1000, "518880.SH": 0}, positions={"510300.SH": 3000, "518880.SH": 800},
        sellable={"510300.SH": 1500, "518880.SH": 800}, sell_anchor={"510300.SH": 4.621, "518880.SH": 9.105},
        attempt=1, policy=P)
    by = {o.symbol: o for o in orders}
    assert by["510300.SH"].quantity == 1500 and by["510300.SH"].limit_price == 4.529
    assert by["518880.SH"].quantity == 800
    assert deferrals[0].symbol == "510300.SH" and deferrals[0].reason == "NOT_SELLABLE" and deferrals[0].quantity == 500

def test_buy_session_allocates_cash_and_skips_untradable():
    orders, deferrals = plan_buy_session(
        target={"513100.SH": 1000, "159915.SZ": 400}, positions={}, cash=1500.0,
        buy_anchor={"513100.SH": 2.196, "159915.SZ": 3.330}, tradable={"513100.SH"}, policy=P)
    assert [o.symbol for o in orders] == ["513100.SH"]
    assert orders[0].quantity == 600 and orders[0].limit_price == 2.228
    reasons = {(d.symbol, d.reason) for d in deferrals}
    assert ("159915.SZ", "SUSPENDED") in reasons and ("513100.SH", "CASH") in reasons

def test_october_schedule_matches_design_table():
    cal = ["20260929", "20260930", "20261008", "20261009", "20261012", "20261013", "20261014", "20261015"]
    assert session_schedule(cal, "20260930", 3) == [
        Session(1, "20261008", "SELL", 0), Session(2, "20261009", "BUY", None),
        Session(3, "20261012", "SELL", 1), Session(4, "20261013", "BUY", None)]
```

- [ ] **步骤 2：运行测试，确认失败**

运行：`cd /d/server/v2.3/server && PYTHONPATH=.. python -m pytest tests/unit/test_oms_planner.py -q -o addopts=''`
预期：ImportError（`app.oms.planner` 不存在）。

- [ ] **步骤 3：实现 `app/oms/planner.py`**

```python
"""Pure two-phase execution planner (policy C3). No database, no network.

Shared by the live execution core and reports/hydra_policy_20261002/policy_replay.py
so the backtest and production compute identical lot targets, limits and orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
from typing import Mapping, Sequence

LOT = 100
TICK = .001
POLICY_ID = "HYDRA_TWO_PHASE_C3_V1"


@dataclass(frozen=True)
class Policy:
    policy_id: str = POLICY_ID
    lot: str = "nearest"
    size_factor: float = 1.001
    default_buy_bps: float = 50.
    buy_bps: tuple = (("518880.SH", 100.), ("513100.SH", 150.), ("513500.SH", 150.))
    sell_schedule: tuple = (50., 200.)
    window: int = 3
    reserve: float = 0.

    def buy_band(self, symbol: str) -> float:
        return dict(self.buy_bps).get(symbol, self.default_buy_bps)

    def sell_band(self, attempt: int) -> float:
        return self.sell_schedule[min(attempt, len(self.sell_schedule) - 1)]


@dataclass(frozen=True)
class PlannedOrder:
    symbol: str
    side: str
    quantity: int
    limit_price: float
    reference_price: float


@dataclass(frozen=True)
class Deferral:
    symbol: str
    side: str
    quantity: int
    reason: str


@dataclass(frozen=True)
class Session:
    seq: int
    trade_date: str
    phase: str
    sell_attempt: int | None


def lot_target(nav, weights, prices, policy) -> dict[str, int]:
    """Whole-lot target. ``nearest`` adds a lot only while it cuts the error and
    the whole basket, grossed up by ``size_factor``, still fits the budget."""
    investable = nav * (1 - policy.reserve)
    target = {s: (math.floor(investable * w / prices[s] / policy.size_factor / LOT) * LOT if w > 0 else 0)
              for s, w in weights.items()}
    if policy.lot == "floor":
        return target
    if policy.lot != "nearest":
        raise ValueError(policy.lot)
    spent = sum(q * prices[s] for s, q in target.items() if q) * policy.size_factor
    while True:
        best, best_gain = None, 0.
        for s in sorted(weights):
            if weights[s] <= 0:
                continue
            gap = investable * weights[s] - target[s] * prices[s]
            step = LOT * prices[s]
            gain = gap - abs(gap - step)
            if gain > best_gain + 1e-9 and spent + step * policy.size_factor <= investable + 1e-9:
                best, best_gain = s, gain
        if best is None:
            return target
        target[best] += LOT
        spent += LOT * prices[best] * policy.size_factor


def buy_limit(anchor: float, bps: float) -> float:
    return math.floor(anchor * (1 + bps / 1e4) / TICK + 1e-8) * TICK


def sell_limit(anchor: float, bps: float) -> float:
    return math.ceil(anchor * (1 - bps / 1e4) / TICK - 1e-8) * TICK


def _number(value) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("Allocation inputs must be finite and nonnegative")
    return number


def _cost(order, quantity) -> int:
    if not quantity:
        return 0
    notional = _number(order["limit_price"]) * quantity
    return int(((notional + max(Decimal(5), notional / 1000)) * 100).to_integral_value(rounding=ROUND_CEILING))


def allocate_buys(orders, cash, lot_size, priorities):
    """Priority tiers, proportional lots within a tier, then largest remainder.
    Verbatim port of reports/hydra_fill_replay_20261002/allocation_code.py."""
    budget = int((_number(cash) * 100).to_integral_value(rounding=ROUND_FLOOR))
    if not isinstance(lot_size, int) or isinstance(lot_size, bool) or lot_size <= 0:
        raise ValueError("Invalid lot size")
    if len({o["symbol"] for o in orders}) != len(orders):
        raise ValueError("Duplicate allocation symbol")
    default_priority = max(priorities.values(), default=0) + 1
    result = []
    for tier in sorted({priorities.get(o["symbol"], default_priority) for o in orders}):
        group = sorted((dict(o) for o in orders if priorities.get(o["symbol"], default_priority) == tier),
                       key=lambda o: o["symbol"])
        for order in group:
            if (order["direction"] != "BUY" or type(order["quantity"]) is not int or order["quantity"] <= 0
                    or order["quantity"] % lot_size or _number(order["limit_price"]) <= 0):
                raise ValueError("Invalid buy allocation")
        scale_base = 10**12

        def quantities(scale):
            return [o["quantity"] * scale // scale_base // lot_size * lot_size for o in group]

        low, high = 0, scale_base
        while low < high:
            middle = (low + high + 1) // 2
            if sum(_cost(o, q) for o, q in zip(group, quantities(middle))) <= budget:
                low = middle
            else:
                high = middle - 1
        allocated = quantities(low)
        budget -= sum(_cost(o, q) for o, q in zip(group, allocated))
        remainders = sorted(range(len(group)), key=lambda i: (
            -(group[i]["quantity"] * low - allocated[i] * scale_base), group[i]["symbol"]))
        for index in remainders:
            order, quantity = group[index], allocated[index]
            if quantity >= order["quantity"]:
                continue
            increment = _cost(order, quantity + lot_size) - _cost(order, quantity)
            if increment <= budget:
                allocated[index] += lot_size
                budget -= increment
        result.extend({**o, "quantity": q} for o, q in zip(group, allocated) if q)
    return sorted(result, key=lambda o: o["symbol"])


def plan_sell_session(*, target: Mapping[str, int], positions: Mapping[str, int], sellable: Mapping[str, int],
                      sell_anchor: Mapping[str, float], attempt: int, policy: Policy):
    band = policy.sell_band(attempt)
    orders, deferrals = [], []
    for s in sorted(target):
        excess = int(positions.get(s, 0)) - int(target[s])
        if excess <= 0:
            continue
        can = min(excess, int(sellable.get(s, 0)))
        if can < excess:
            deferrals.append(Deferral(s, "SELL", excess - can, "NOT_SELLABLE"))
        if can > 0:
            orders.append(PlannedOrder(s, "SELL", can, sell_limit(float(sell_anchor[s]), band), float(sell_anchor[s])))
    return orders, deferrals


def plan_buy_session(*, target: Mapping[str, int], positions: Mapping[str, int], cash: float,
                     buy_anchor: Mapping[str, float], tradable, policy: Policy):
    orders, deferrals, wanted = [], [], []
    for s in sorted(target):
        need = (int(target[s]) - int(positions.get(s, 0))) // LOT * LOT
        if need <= 0:
            continue
        if s not in tradable:
            deferrals.append(Deferral(s, "BUY", need, "SUSPENDED"))
            continue
        wanted.append(dict(symbol=s, direction="BUY", quantity=need,
                           limit_price=buy_limit(float(buy_anchor[s]), policy.buy_band(s))))
    allocated = ({o["symbol"]: o["quantity"] for o in allocate_buys(wanted, max(0., float(cash)), LOT, {})}
                 if wanted else {})
    for o in wanted:
        q = allocated.get(o["symbol"], 0)
        if q < o["quantity"]:
            deferrals.append(Deferral(o["symbol"], "BUY", o["quantity"] - q, "CASH"))
        if q:
            orders.append(PlannedOrder(o["symbol"], "BUY", q, o["limit_price"], float(buy_anchor[o["symbol"]])))
    return orders, deferrals


def _day(value: str) -> date:
    return date(int(value[:4]), int(value[4:6]), int(value[6:]))


def _adjacent(a: str, b: str) -> bool:
    return (_day(b) - _day(a)).days == 1


def session_schedule(calendar: Sequence[str], signal_date: str, window: int) -> list[Session]:
    """Close-sell / next-natural-day open-buy sessions; mirrors policy_replay eligibility."""
    days = sorted(set(calendar))
    later = [d for d in days if d > signal_date]
    first_sell = next((a for a, b in zip(later, later[1:]) if _adjacent(a, b)), None)
    if first_sell is None:
        raise ValueError("no adjacent trading-day pair after signal")
    i_buy = days.index(first_sell) + 1
    if i_buy + window - 1 >= len(days):
        raise ValueError("calendar shorter than execution window")
    sessions, seq, attempt = [], 0, 0
    for i in range(i_buy - 1, i_buy + window):
        d, age = days[i], i - i_buy + 1
        if 1 <= age <= window and _adjacent(days[i - 1], d):
            seq += 1
            sessions.append(Session(seq, d, "BUY", None))
        nxt = days[i + 1] if i + 1 < len(days) else None
        if age < window and nxt is not None and _adjacent(d, nxt):
            seq += 1
            sessions.append(Session(seq, d, "SELL", attempt))
            attempt += 1
    return sessions
```

- [ ] **步骤 4：运行测试，确认通过**

运行同步骤 2。预期：5 passed。如果 `test_nearest_lot_*` 的期望股数和实现不一致，先手工核算，确认后再改测试里的数字；计算口径以实现为准，不改实现去迁就测试。

- [ ] **步骤 5：提交**

```bash
git add v2.3/server/app/oms/__init__.py v2.3/server/app/oms/planner.py v2.3/server/tests/unit/test_oms_planner.py
git commit -m "feat(oms): add pure two-phase planner shared with policy replay"
```

### 任务 2：回测改为导入计划器，锁定口径

**文件：**
- 修改：`reports/hydra_policy_20261002/policy_replay.py`（删掉重复的 `lot_target`、`buy_limit`、`sell_limit`，改为导入；`allocate_buys` 也改为从 planner 导入）
- 测试：`v2.3/server/tests/unit/test_oms_planner_parity.py`

- [ ] **步骤 1：先写测试**

用合成的 3 只 ETF、40 个交易日的日线，加上 2 个月度信号，分别跑 `policy_replay.run`（C3 参数）和一个只用 planner 函数的最小驱动，要求两者逐日的持仓和现金完全相同。

```python
import sys
from pathlib import Path
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "reports" / "hydra_policy_20261002"))
policy_replay = pytest.importorskip("policy_replay")

from app.oms import planner


def _synthetic():
    dates = pd.bdate_range("2026-03-02", periods=40)
    syms = ["510300.SH", "511260.SH", "518880.SH"]
    rows, base = [], {"510300.SH": 4.0, "511260.SH": 130.0, "518880.SH": 9.0}
    for i, d in enumerate(dates):
        for j, s in enumerate(syms):
            p = base[s] * (1 + 0.002 * ((i * (j + 1)) % 7 - 3))
            rows.append(dict(date=d, symbol=s, open=p * 1.001, high=p * 1.01, low=p * 0.99, close=p, volume=1e7, suspendFlag=0))
    raw = pd.DataFrame(rows)
    daily = {(r.date, r.symbol): r._asdict() for r in raw.itertuples(index=False)}
    close = raw.pivot(index="date", columns="symbol", values="close")
    weights = pd.DataFrame([[0.3, 0.6, 0.1], [0.2, 0.7, 0.1]], columns=syms, index=[dates[2], dates[22]])
    return weights, daily, close


def test_replay_uses_planner_functions():
    assert policy_replay.lot_target is planner.lot_target
    assert policy_replay.buy_limit is planner.buy_limit
    assert policy_replay.sell_limit is planner.sell_limit
    assert policy_replay.allocate_buys is planner.allocate_buys


def test_c3_schedule_matches_replay_sessions():
    weights, daily, close = _synthetic()
    c3 = policy_replay.Policy("C3", lot="nearest", size_factor=1.001, buy_bps=planner.Policy().buy_bps,
                              touch=True, sell_schedule=(50., 200.))
    _, _, _, fills, _ = policy_replay.run(weights, daily, close, [], c3, capital=200000.)
    cal = [d.strftime("%Y%m%d") for d in close.index]
    planned_days = set()
    for signal in weights.index:
        for sess in planner.session_schedule(cal, signal.strftime("%Y%m%d"), 3):
            planned_days.add((sess.trade_date, "close" if sess.phase == "SELL" else "open"))
    fill_days = {(pd.Timestamp(f.date).strftime("%Y%m%d"), f.phase) for f in fills.itertuples()}
    assert fill_days <= planned_days
```

- [ ] **步骤 2：运行，确认 `test_replay_uses_planner_functions` 失败**（回测里现在还是自己的副本）。
- [ ] **步骤 3：修改 `policy_replay.py`**：删除本地的 `lot_target`、`buy_limit`、`sell_limit` 定义，把 `from allocation_code import allocate_buys` 换成：

```python
from app.oms.planner import allocate_buys, buy_limit, lot_target, sell_limit  # one implementation for backtest and live
```

文件头注明：运行时需要 `PYTHONPATH` 包含 `v2.3/server`。

- [ ] **步骤 4：运行测试，确认通过；然后到服务器复跑 C3，结果必须和改动前逐位一致**

```bash
RUN=/opt/qmt-server/private/research-runs/hydra-policy-20261002-c1
ssh qmt "mkdir -p $RUN/srv/app/oms" && scp -q v2.3/server/app/oms/__init__.py v2.3/server/app/oms/planner.py qmt:$RUN/srv/app/oms/ && ssh qmt "touch $RUN/srv/app/__init__.py" && scp -q reports/hydra_policy_20261002/policy_replay.py qmt:$RUN/
ssh qmt "cd $RUN && cp summary3.json summary3.before.json && PYTHONPATH=$RUN/srv /opt/qmt-server/venv/bin/python run_policy_grid3.py | tail -1 && cmp summary3.before.json summary3.json && echo IDENTICAL"
```

预期：输出 `IDENTICAL`，说明改用 planner 之后，回测结果和改动前逐字节一致（`summary3.json` 里记录的源码哈希不同，所以如果 `cmp` 报差异，就用 `python -c` 比较各 panel 的 `cagr` 和 `rebalances` 字段，结果必须全部相等）。

- [ ] **步骤 5：提交** `git commit -m "refactor(research): import planner from app.oms so replay and live share code"`

### 任务 3：订单状态机

**文件：** 新建 `app/oms/states.py`；测试 `tests/unit/test_oms_states.py`

- [ ] **步骤 1：先写测试**

```python
import pytest
from app.oms.states import IllegalTransition, OrderState as S, classify_qmt, legacy_status, transition

@pytest.mark.parametrize("status,traded,ordered,expected", [
    (48, 0, 100, S.ACKED), (49, 0, 100, S.ACKED), (50, 0, 100, S.ACKED), (55, 40, 100, S.PARTIAL),
    (51, 0, 100, S.PENDING_CANCEL), (52, 40, 100, S.PENDING_CANCEL), (53, 40, 100, S.CANCELLED),
    (54, 0, 100, S.CANCELLED), (56, 100, 100, S.FILLED), (57, 0, 100, S.REJECTED), (255, 0, 100, S.UNKNOWN),
    (50, 100, 100, S.FILLED)])
def test_classify(status, traded, ordered, expected):
    assert classify_qmt(status, traded, ordered) is expected

def test_terminal_is_sticky_and_idempotent():
    assert transition(S.FILLED, S.FILLED) is S.FILLED
    with pytest.raises(IllegalTransition):
        transition(S.FILLED, S.CANCELLED)

def test_unknown_never_goes_back_to_planned_and_can_be_resolved():
    assert transition(S.UNKNOWN, S.ACKED) is S.ACKED
    assert transition(S.UNKNOWN, S.NOT_SUBMITTED) is S.NOT_SUBMITTED
    with pytest.raises(IllegalTransition):
        transition(S.UNKNOWN, S.PLANNED)

def test_pending_cancel_is_not_terminal():
    assert transition(S.PENDING_CANCEL, S.CANCELLED) is S.CANCELLED
    assert transition(S.PENDING_CANCEL, S.FILLED) is S.FILLED

def test_legacy_mapping():
    assert legacy_status(S.EXPIRED_DAY, 300) == "CANCELLED"
    assert legacy_status(S.ACKED, 0) is None
    assert legacy_status(S.ACKED, 200) == "PARTIAL"
    assert legacy_status(S.NOT_SUBMITTED, 0) == "NOT_SUBMITTED"
```

- [ ] **步骤 2：运行，确认失败。**
- [ ] **步骤 3：实现**

```python
"""Order state machine for the execution core. The only place that decides legality."""
from __future__ import annotations

from enum import Enum


class OrderState(str, Enum):
    PLANNED = "PLANNED"
    SUBMITTING = "SUBMITTING"
    ACKED = "ACKED"
    PARTIAL = "PARTIAL"
    PENDING_CANCEL = "PENDING_CANCEL"
    UNKNOWN = "UNKNOWN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED_DAY = "EXPIRED_DAY"
    NOT_SUBMITTED = "NOT_SUBMITTED"


S = OrderState
TERMINAL = frozenset({S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED_DAY, S.NOT_SUBMITTED})
_LIVE = {S.ACKED, S.PARTIAL, S.PENDING_CANCEL, S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED_DAY}
ALLOWED = {
    S.PLANNED: {S.SUBMITTING, S.NOT_SUBMITTED},
    S.SUBMITTING: _LIVE | {S.UNKNOWN, S.NOT_SUBMITTED},
    S.UNKNOWN: _LIVE | {S.NOT_SUBMITTED},
    S.ACKED: _LIVE,
    S.PARTIAL: _LIVE - {S.ACKED, S.REJECTED},
    S.PENDING_CANCEL: {S.PENDING_CANCEL, S.PARTIAL, S.CANCELLED, S.FILLED, S.EXPIRED_DAY},
}


class IllegalTransition(ValueError):
    pass


def transition(current: OrderState, new: OrderState) -> OrderState:
    if new is current:
        return current
    if current in TERMINAL or new not in ALLOWED.get(current, set()):
        raise IllegalTransition(f"{current.value} -> {new.value}")
    return new


def classify_qmt(status: int, traded: int, ordered: int) -> OrderState:
    if ordered > 0 and traded >= ordered:
        return S.FILLED
    return {48: S.ACKED, 49: S.ACKED, 50: S.ACKED, 55: S.PARTIAL, 51: S.PENDING_CANCEL, 52: S.PENDING_CANCEL,
            53: S.CANCELLED, 54: S.CANCELLED, 56: S.FILLED, 57: S.REJECTED}.get(int(status), S.UNKNOWN)


def legacy_status(state: OrderState, filled: int) -> str | None:
    if state is S.FILLED:
        return "FILLED"
    if state in (S.CANCELLED, S.EXPIRED_DAY):
        return "CANCELLED"
    if state is S.REJECTED:
        return "REJECTED" if not filled else "CANCELLED"
    if state is S.NOT_SUBMITTED:
        return "NOT_SUBMITTED"
    return "PARTIAL" if filled > 0 else None
```

- [ ] **步骤 4：运行，确认通过。**
- [ ] **步骤 5：提交** `feat(oms): add order state machine and QMT status mapping`

### 任务 4：数据表

**文件：** 新建 `app/oms/models.py`；修改 `app/models/__init__.py`，在已有导入后追加 `from app.oms.models import *  # noqa: E402,F401,F403`；测试 `tests/unit/test_oms_models.py`

- [ ] **步骤 1：先写测试**：`init_db` 建表后，七张 `oms_*` 表都存在；`oms_fills` 对（account_alias, broker_trade_id）的唯一约束生效；`oms_sessions` 对（cycle_id, seq）的唯一约束生效。

```python
import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from app.db import init_db, make_engine, make_session_factory
from app.oms.models import OmsFill, OmsSession

def test_tables_and_uniques(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/oms.db"); init_db(engine)
    names = set(inspect(engine).get_table_names())
    assert {"oms_target_versions", "oms_cycles", "oms_sessions", "oms_orders", "oms_order_events",
            "oms_fills", "oms_broker_snapshots", "oms_reconciliations"} <= names
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(OmsFill(account_alias="a", broker_trade_id="t1", symbol="510300.SH", side="BUY", quantity=100,
                      price=4.6, traded_at="x", received_at="x"))
        s.add(OmsFill(account_alias="a", broker_trade_id="t1", symbol="510300.SH", side="BUY", quantity=100,
                      price=4.6, traded_at="x", received_at="x"))
        with pytest.raises(IntegrityError):
            s.commit()
```

- [ ] **步骤 2：运行，确认失败。**
- [ ] **步骤 3：实现**：时间统一存 ISO 字符串，日期存 YYYYMMDD 字符串，JSON 列用 `JSON`，风格与 `app/models/emergency_execution.py` 一致。

```python
"""Execution-core tables. Only app.oms.ledger / app.oms.cycles write them."""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base

__all__ = ["OmsTargetVersion", "OmsCycle", "OmsSession", "OmsOrder", "OmsOrderEvent", "OmsFill",
           "OmsBrokerSnapshot", "OmsReconciliation"]


class OmsTargetVersion(Base):
    __tablename__ = "oms_target_versions"
    __table_args__ = (UniqueConstraint("instance_id", "signal_date", "source_sha256"),)
    target_version_id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    signal_date: Mapped[str] = mapped_column(String, nullable=False)
    weights: Mapped[dict] = mapped_column(JSON, nullable=False)
    signal_closes: Mapped[dict] = mapped_column(JSON, nullable=False)
    calendar: Mapped[list] = mapped_column(JSON, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class OmsCycle(Base):
    __tablename__ = "oms_cycles"
    cycle_id: Mapped[str] = mapped_column(String, primary_key=True)
    cycle_no: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    target_version_id: Mapped[str] = mapped_column(String, nullable=False)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False)
    policy: Mapped[dict] = mapped_column(JSON, nullable=False)
    nav_at_signal: Mapped[float] = mapped_column(Float, nullable=False)
    frozen_target: Mapped[dict] = mapped_column(JSON, nullable=False)
    sell_anchor: Mapped[dict] = mapped_column(JSON, nullable=False)
    buy_anchor: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    lot_gap: Mapped[float] = mapped_column(Float, nullable=False)
    schedule: Mapped[list] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    close_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class OmsSession(Base):
    __tablename__ = "oms_sessions"
    __table_args__ = (UniqueConstraint("cycle_id", "seq"),)
    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_date: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    sell_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    plan_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    basis_snapshot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    deferrals: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class OmsOrder(Base):
    __tablename__ = "oms_orders"
    client_order_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    cycle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[float] = mapped_column(Float, nullable=False)
    reference_price: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    filled_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.)
    projected_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projected_status: Mapped[str | None] = mapped_column(String, nullable=True)
    last_observed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class OmsOrderEvent(Base):
    __tablename__ = "oms_order_events"
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[str] = mapped_column(String, nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)


class OmsFill(Base):
    __tablename__ = "oms_fills"
    __table_args__ = (UniqueConstraint("account_alias", "broker_trade_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False)
    broker_trade_id: Mapped[str] = mapped_column(String, nullable=False)
    client_order_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    traded_at: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[str] = mapped_column(String, nullable=False)


class OmsBrokerSnapshot(Base):
    __tablename__ = "oms_broker_snapshots"
    snapshot_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_alias: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    trade_date: Mapped[str] = mapped_column(String, nullable=False)
    taken_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    received_at: Mapped[str] = mapped_column(String, nullable=False)


class OmsReconciliation(Base):
    __tablename__ = "oms_reconciliations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(String, nullable=False)
    cycle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    discrepancies: Mapped[list] = mapped_column(JSON, nullable=False)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
```

注意：`app/models/__init__.py` 定义了 `Base`，而 `app/oms/models.py` 又从它导入 `Base`。所以导入语句必须放在 `__init__.py` 末尾，放在所有其他模型导入之后，避免循环导入。

- [ ] **步骤 4：运行，确认通过；再跑 `tests/unit/test_hydra_relay.py`，确认没有破坏建表。**
- [ ] **步骤 5：提交** `feat(oms): add execution core tables`

### 任务 5：接口数据结构

**文件：** 新建 `app/oms/schemas.py`；测试 `tests/unit/test_oms_schemas.py`（验证：时间必须带时区；YYYYMMDD 格式；`side` 只能是 BUY/SELL；`kind` 枚举）。

```python
"""Wire format between the Windows agent and the server execution core."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Date8 = Field(pattern=r"^\d{8}$")


class BrokerOrder(BaseModel):
    broker_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(ge=0)
    price: float = Field(ge=0)
    traded_volume: int = Field(ge=0)
    traded_price: float = Field(ge=0)
    status: int
    remark: str = ""
    status_msg: str = ""


class BrokerTrade(BaseModel):
    broker_trade_id: str
    broker_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    traded_at: str
    remark: str = ""


class Quote(BaseModel):
    last_price: float = Field(gt=0)
    is_trading: bool = True


class SnapshotIn(BaseModel):
    account_alias: str
    kind: Literal["PRE", "EOD", "ADHOC"]
    trade_date: str = Date8
    taken_at: datetime
    available_cash: float = Field(ge=0)
    total_asset: float = Field(ge=0)
    positions: dict[str, int]
    sellable: dict[str, int]
    orders: list[BrokerOrder]
    trades: list[BrokerTrade] | None = None
    quotes: dict[str, Quote] = Field(default_factory=dict)

    @field_validator("taken_at")
    @classmethod
    def zoned(cls, v: datetime) -> datetime:
        if v.utcoffset() is None:
            raise ValueError("taken_at must carry a timezone")
        return v


class EventIn(BaseModel):
    event_id: str = Field(min_length=8)
    client_order_id: str
    kind: Literal["SUBMIT_STARTED", "ACKED", "SUBMIT_REJECTED", "SUBMIT_UNKNOWN", "CANCEL_REQUESTED"]
    observed_at: datetime
    broker_order_id: str | None = None
    detail: str | None = None


class EventsIn(BaseModel):
    account_alias: str
    events: list[EventIn]


class PlanOrder(BaseModel):
    client_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    limit_price: float


class PlanOut(BaseModel):
    account_alias: str
    cycle_id: str
    session_id: str
    trade_date: str
    phase: Literal["SELL", "BUY"]
    executable: bool
    plan_sha256: str
    frozen_target: dict[str, int]
    orders: list[PlanOrder]
```

- [ ] 写测试 → 失败 → 实现 → 通过 → 提交 `feat(oms): add agent/server wire schemas`

### 任务 6：订单账本

**文件：** 新建 `app/oms/ledger.py`；测试 `tests/unit/test_oms_ledger.py`

**职责与签名**：

```python
class OrderLedger:
    def __init__(self, session_factory, settlement: SettlementService): ...
    def create_orders(self, session, *, cycle: OmsCycle, oms_session: OmsSession,
                      planned: list[PlannedOrder], instance_id: str, now: str) -> list[OmsOrder]
        # 订单号 H{cycle_no:05d}{seq:02d}{leg:02d}，leg 接着本时段已有的最大值继续编。
        # 同时写旧系统镜像：RawSignal(signal_id=uuid5(NAMESPACE_URL, client_order_id).hex, instance_id, PASS)，
        # Order(order_id=client_order_id, execution_domain="live", qmt_account_alias, target_id=cycle.target_version_id,
        #       rebalance_id=cycle.cycle_id, attempt_id=None, batch_sha256=session plan sha, status="OMS_ROUTED",
        #       valid_date=trade_date, execution_reference_price=reference)，OrderSignalMap(signal_quantity=quantity)。
        # 镜像状态 OMS_ROUTED 永远不会被旧接口 /orders 下发，因为旧接口只发 PENDING。
    def apply_events(self, account_alias: str, events: list[EventIn], now: str) -> dict
        # 按 event_id 幂等（已存在则 duplicate+1）；按事件种类转移状态：
        #   SUBMIT_STARTED→SUBMITTING，ACKED→ACKED（记 broker_order_id），SUBMIT_REJECTED→REJECTED，
        #   SUBMIT_UNKNOWN→UNKNOWN，CANCEL_REQUESTED 只记录不改状态。
        #   非法转移：applied=False，error 写原因，不抛错。
    def observe_orders(self, account_alias: str, orders: list[BrokerOrder], observed_at: datetime) -> dict
        # 按 remark==client_order_id 匹配（委托备注被截断时按前缀匹配，10 个字符不会被截断）；
        # 已成交数量只增不减；状态用 classify_qmt 映射后调用 transition；匹配不上的记为外部委托，写进返回值。
    def record_trades(self, account_alias: str, trades: list[BrokerTrade], now: str) -> int
        # 写入 oms_fills；靠唯一约束去重；按 remark 关联 client_order_id，关联不上就置空，表示外部成交。
    def finalize_day(self, account_alias: str, trade_date: str, orders: list[BrokerOrder], observed_at: datetime) -> list[str]
        # 只在 15:00 之后调用。对当日所有非终态订单：
        #   在券商当日委托里，状态为 50/55 → EXPIRED_DAY；
        #   不在券商当日委托里，且状态为 PLANNED/SUBMITTING/UNKNOWN → NOT_SUBMITTED（证据：日终委托清单）。
    def project(self, account_alias: str, trade_date: str) -> dict
        # 对 (filled_qty, legacy_status) 不等于 (projected_qty, projected_status) 的订单，生成 TradeResult(
        #   order_id=client_order_id, filled_quantity=filled_qty, filled_price=avg_price,
        #   filled_time=last_observed_at, status=legacy_status, symbol, direction=side, qmt_order_id=broker_order_id,
        #   not_submitted_reason="EXECUTION_WINDOW_EXPIRED" if NOT_SUBMITTED)，
        # 调用 settlement.settle(trade_date, results, "live", (account_alias,))，成功后更新 projected_*。
```

- [ ] **步骤 1：先写测试**（用临时数据库，结构参照 `tests/unit/test_hydra_relay.py::_setup`：新建 `InstanceState(live_hydra, ledger_mode="attributed", virtual_cash=200000)`）。至少覆盖下面 8 个用例：

```python
def test_create_orders_writes_mirror_rows_not_visible_to_legacy_orders_api(...): ...
    # 建 2 笔单后：OmsOrder 2 条，Order 2 条且 status=="OMS_ROUTED"；
    # OrdersQueueService(sf).list_pending(trade_date, "live", ("hydra-live",)) == []
def test_events_are_idempotent_and_illegal_transition_is_recorded_not_raised(...): ...
def test_observe_orders_maps_status_and_never_decreases_filled(...): ...
def test_finalize_day_expires_status_50_after_close_and_marks_missing_as_not_submitted(...): ...
def test_project_updates_instance_state_exactly_once(...): ...
    # 观察到成交 1000 股 @ 2.225 → project → InstanceState.virtual_positions["513100.SH"] == 1000；
    # 再 project 一次 → 不重复入账
def test_partial_then_expired_projects_cancelled_with_filled_quantity(...): ...
def test_trades_dedupe_on_broker_trade_id_and_flag_external(...): ...
def test_remark_truncated_to_24_still_matches(...): ...
```

每个用例都要写完整的准备、执行、断言，不能只写函数名；上面注释给出了关键断言。

- [ ] 运行 → 失败 → 实现 → 通过 → 提交 `feat(oms): add single-writer order ledger with legacy ledger projection`

### 任务 7：调仓周期服务

**文件：** 新建 `app/oms/cycles.py`；测试 `tests/unit/test_oms_cycles.py`

**签名与流程**：

```python
class CycleService:
    def __init__(self, session_factory, ledger: OrderLedger, reconcile: ReconcileService, policy: Policy = Policy()): ...
    def publish_target(self, *, instance_id, account_alias, signal_date, weights, signal_closes, calendar,
                       source_sha256, now) -> dict
        # 先在 BEGIN IMMEDIATE 事务里检查本账户有没有 OPEN（ACTIVE/HELD/PENDING_APPROVAL）的周期，有就报 409。
        # 新建目标版本；把同一实例的旧版本标记为 SUPERSEDED。
        # nav = 策略现金 + Σ 策略持仓 × 信号日收盘价（读 InstanceState）。
        # frozen = lot_target(nav, weights, signal_closes, policy)；schedule = session_schedule(calendar, signal_date, window)。
        # lot_gap = Σ max(0, w - frozen[s]×p/nav)。
        # 新建周期：cycle_no = max+1，cycle_id = f"C{cycle_no:05d}"，status=PENDING_APPROVAL；sell_anchor = signal_closes。
        # 用"假想的 EOD"生成第一个时段（首个卖出日的 SELL）的计划，状态 PLANNED。
        # 返回逐股清单：标的、目标股数、现有持仓、差额、首次卖单。
    def approve(self, cycle_id: str, approver: str, now: str) -> None           # PENDING_APPROVAL → ACTIVE
    def ingest_snapshot(self, snap: SnapshotIn, now: str) -> dict
        # 1）按 sha256 存快照（幂等）。
        # 2）ledger.observe_orders；如有成交明细，ledger.record_trades。
        # 3）kind=EOD 且 taken_at ≥ 15:00：ledger.finalize_day，再 ledger.project。
        # 4）对账：reconcile.reconcile_total(snap.positions 只取白名单 9 只, snap.available_cash, taken_at, 0.0, "live", alias)；
        #    另外检查当日有没有未知的挂单。结果写入 oms_reconciliations。
        # 5）没有 ACTIVE 周期：只返回对账结果。
        # 6）对账不通过：周期 → HELD，返回 hold。
        # 7）kind=EOD 且通过：关闭今天的时段；如果今天是首个卖出日，buy_anchor = {s: quotes[s].last_price}（缺任何一只就暂停）；
        #    下一个交易日如果有时段，就用最新的策略持仓、可卖数量（取 min(ledger, snap.sellable)）、
        #    现金（取 min(ledger cash, snap.available_cash)）生成计划；
        #    日程走完：关闭周期（WINDOW_COMPLETE），写报告。
        # 返回 {"reconciliation": {...}, "planned_sessions": [...], "cycle_status": ...}
    def plan_for(self, account_alias: str, trade_date: str, phase: str, executable: bool) -> PlanOut
        # 只返回 PLANNED 状态的时段；plan_sha256 = sha256(规范化 JSON(订单))；周期不是 ACTIVE 时 executable=False。
    def report(self, cycle_id: str) -> dict
        # lot_gap、执行欠配（按收盘时最新的快照估值）、按原因分类的未完成部分（来自 deferrals 和最终剩余）、
        # 成交价相对锚定价的偏离（bps）、各时段的订单清单。
```

- [ ] **步骤 1：先写测试**：用临时数据库加一个小的模拟快照序列，覆盖：
  1. `publish_target` 生成的周期是 PENDING_APPROVAL，第一时段是 10/8 的卖出；`plan_for` 返回 executable=False；
  2. `approve` 之后 executable=True；
  3. 10/8 的 EOD 快照：卖单 56 全部成交 → 生成 10/9 的买入计划，buy_anchor 来自报价，现金 = min（账本，券商）；
  4. 快照持仓和账本不一致 → 周期 HELD，不生成计划；
  5. 窗口走完 → 周期关闭，报告里有 lot_gap 和执行欠配；
  6. 已有 OPEN 周期时再次 `publish_target` → 409。

  每个用例写完整的准备和断言。

- [ ] 运行 → 失败 → 实现 → 通过 → 提交 `feat(oms): add snapshot-driven cycle service`

### 任务 8：接口、鉴权、开关

**文件：**
- 新建：`app/oms/api.py`
- 修改：`app/auth.py`（在 `live_exact_paths` 里加 `"/oms/live/snapshot"`、`"/oms/live/plan"`、`"/oms/live/events"`、`"/oms/live/status"`）
- 修改：`app/settings.py`（在 `live_order_generation_enabled` 附近加 `oms_live_enabled: bool = False`）
- 修改：`app/main.py`（`from app.oms import api as oms_api`；`app.include_router(oms_api.router, tags=["oms"])`）
- 测试：`tests/unit/test_oms_api.py`

```python
router = APIRouter(prefix="/oms/live")

def _authorize(auth: AuthContext, account_alias: str) -> None:
    if auth.execution_domain != "live" or auth.client_id == "live-trigger" or not auth.allows_account(account_alias):
        raise APIError(ErrorCode.AUTH_FAILED, "OMS 仅限 live 客户端及其账户", http_status=403)

@router.post("/snapshot")   -> APIResponse[dict]   # CycleService.ingest_snapshot；开关关闭时照常处理（用于只读演练）
@router.get("/plan")        -> APIResponse[PlanOut] # account_alias, trade_date, phase；executable = settings.oms_live_enabled and cycle ACTIVE
@router.post("/events")     -> APIResponse[dict]   # OrderLedger.apply_events
@router.get("/status")      -> APIResponse[dict]   # 当前周期、时段、订单、最近一次对账
```

服务对象的构造放在 `app/dependencies.py`：新增 `get_oms_cycle_service(sf, settings)`，内部复用 `get_settlement_service` 的费率参数和 `ReconcileService(sf)`。

- [ ] **步骤 1：先写测试**（参照 `tests/unit/test_live_trigger.py` 的 Settings 写法）：
  1. 实盘密钥能访问四个路径，模拟盘密钥返回 403，触发密钥返回 403；
  2. 账户别名不在白名单 → 403；
  3. 开关关闭时 plan 的 executable=False；打开后，周期为 ACTIVE 时 executable=True；
  4. snapshot 用同一份数据重复上传 → 只存一份。
- [ ] 运行 → 失败 → 实现 → 通过 → 提交 `feat(oms): expose /oms/live API behind live auth and feature flag`

### 任务 9：危险入口改造

**文件：** 修改 `app/api/live_trigger.py`、`app/scheduler/pipeline.py`（`run` 入口）、`app/api/orders.py`、`app/api/trade_result.py`、`app/api/hydra_relay.py`（`/hydra/targets/stage`、`/hydra/rebalances/retry`、`/hydra/execution/advance`、`/hydra/attempts/close`）；测试 `tests/unit/test_oms_guards.py`，同时修改受影响的 `tests/unit/test_live_trigger.py`。

- [ ] **步骤 1：先写测试**：
  1. `POST /hydra/live/trigger` 返回 HTTP 410，不调用 pipeline（用 `SpyPipeline` 断言它没有被调用）；
  2. `StrategyPipeline.run(..., execution_domain="live")` 返回 `{"skipped": "live_domain_owned_by_oms"}`，并且没有任何订单被修改（准备一笔 live PENDING 单，调用后状态不变）；
  3. 开关打开时，实盘密钥访问 `GET /orders`、`POST /trade-result`、四个旧 Hydra 实盘写接口都返回 409；开关关闭时行为不变。
- [ ] **步骤 2：实现**：
  - `live_trigger.py`：鉴权检查之后直接 `raise APIError(ErrorCode.BAD_REQUEST, "实盘管线入口已停用；实盘执行由 /oms/live 负责", http_status=410)`。
  - `pipeline.py` 的 `run`：在取锁之前，如果 `execution_domain == "live"`，记日志后返回上面的 skipped 字典。
  - 其余接口：在 handler 开头加 `if auth.execution_domain == "live" and settings.oms_live_enabled: raise APIError(ErrorCode.BAD_REQUEST, "实盘由执行核心接管", http_status=409)`。
- [ ] **步骤 3：跑这些测试，再跑 `tests/unit/test_live_trigger.py tests/unit/test_execution_domain_isolation.py tests/unit/test_hydra_relay.py`。**
- [ ] **步骤 4：提交** `fix(live): retire live pipeline trigger and hand live writes to the execution core`

### 任务 10：发布目标、批准、人工处理脚本

**文件：** 新建 `scripts/oms_publish_target.py`、`scripts/oms_approve_cycle.py`、`scripts/oms_resolve.py`；测试 `tests/unit/test_oms_scripts.py`

- `oms_publish_target.py --instance live_hydra_v481_rb --account hydra-live --signal-date 20260930 --weights weights.json --closes closes.json --calendar calendar.json --source-sha256 <sha> [--apply]`：不带 `--apply` 时只打印清单（dry-run）；带 `--apply` 时调用 `CycleService.publish_target`，打印逐股清单（JSON + 对齐的表格）。
- `oms_approve_cycle.py --cycle-id C00001 --approver <名字>`。
- `oms_resolve.py --snapshot-id <sha> --resolution "<原因>" --operator <名字>`：把对应的 `oms_reconciliations` 记录标记为已处理，周期 HELD → ACTIVE，并按最新快照重新生成下一时段计划。
- [ ] 测试（临时数据库）：dry-run 不写库；`--apply` 生成周期；批准后状态 ACTIVE；`resolve` 放行后能生成计划。→ 实现 → 提交 `feat(oms): add publish/approve/resolve operator scripts`

### 任务 11：客户端本地日志

**文件：** 新建 `live_client/oms_journal.py`；测试 `live_client/tests/test_oms_journal.py`

```python
class OmsJournal:
    def __init__(self, path: Path): ...                       # 每个事务新开连接，BEGIN IMMEDIATE
    def cache_plan(self, plan: dict) -> None                  # 主键 plan_sha256；同一 session_id 的计划内容不同就报错
    def plan(self, trade_date: str, phase: str) -> dict | None
    def intent(self, client_order_id: str) -> dict | None
    def mark(self, client_order_id: str, state: str, broker_order_id: str | None, detail: str | None) -> dict
        # state ∈ {SUBMITTING, ACKED, REJECTED, UNKNOWN}；
        # 同时写入 outbox 事件（event_id=uuid4().hex，kind 按状态对应：
        #   SUBMITTING→SUBMIT_STARTED, ACKED→ACKED, REJECTED→SUBMIT_REJECTED, UNKNOWN→SUBMIT_UNKNOWN）
    def outbox(self, limit: int = 500) -> list[dict]
    def acknowledge(self, event_ids: list[str]) -> None
    def hold_flag(self) -> bool                                # 本地开关文件 <journal_dir>/HOLD 是否存在
```

- [ ] 测试：计划缓存不可被覆盖；`mark` 产生 outbox 事件；`acknowledge` 后 outbox 为空；进程重启后仍能读到 SUBMITTING 状态。→ 实现 → 提交 `feat(agent): add local OMS journal with outbox`

### 任务 12：网关扩展

**文件：** 修改 `live_client/gateway.py`（`XtQMTGateway` 和 `MockQMTGateway` 都加）；测试 `live_client/tests/test_oms_gateway.py`（使用现有的 `xtquant_stub` 写法，见 `test_live_client.py:28-41`）

```python
def submit_limit(self, *, symbol: str, side: str, quantity: int, limit_price: float, remark: str) -> SubmissionResult
    # order_stock(account, symbol, STOCK_BUY/SELL, qty, FIX_PRICE, limit, "hydra_oms", remark)；
    # 返回值 >0 → SUBMITTED；<0 或 None → REJECTED；抛异常 → UNKNOWN（status="UNKNOWN"）
def day_orders(self) -> list[BrokerOrderSnapshot]          # query_stock_orders(account, False)，复用 _query_orders_for_settlement 的超时机制
def day_trades(self, timeout_seconds: float = 10.0) -> list[dict] | None
    # query_stock_trades_async 加 Event 等待；超时返回 None（已知 MiniQMT 这个查询可能一直不返回）
def quotes(self, symbols: list[str]) -> dict[str, dict]     # 复用 market_quote：{symbol: {"last_price", "is_trading"}}
```

- [ ] 测试：返回值 >0、<0、抛异常三种映射；`day_trades` 超时返回 None；委托备注原样传入。→ 实现 → 提交 `feat(agent): extend QMT gateway for OMS sessions`

### 任务 13：仿真交易所

**文件：** 新建 `live_client/sim_exchange.py`；测试 `live_client/tests/test_sim_exchange.py`

```python
class SimExchange:
    def __init__(self, bars: dict[tuple[str, str], dict], *, cash: float, positions: dict[str, int],
                 participation: float = .01, slip_bps: float = 5., remark_limit: int = 24): ...
    def set_clock(self, trade_date: str, hhmmss: str) -> None
        # 推进时钟时按顺序触发：
        #   09:25 开盘集合竞价撮合（9:15–9:25 报入的单）；
        #   14:55 盘中触价撮合（未成交买单，当日最低价 < 限价就按限价成交，受容量约束）；
        #   15:00 收盘集合竞价撮合（14:57–15:00 报入的单，按收盘价）。
        #   换日时：昨天的可卖数量变成全部持仓（T+1）；当日委托和成交清空（查询只返回当天）。
    def order_stock(self, symbol, side, quantity, price, remark) -> int     # 校验整手、现金冻结、可卖数量、交易时间；不合法返回 -1
    def cancel(self, order_id: int) -> int                                  # 9:20–9:25、14:57–15:00 拒绝；否则先 51/52，下一次推进时钟变 54/53
    def orders(self) -> list[dict]; def trades(self) -> list[dict]; def asset(self) -> dict; def positions(self) -> dict
    faults: set[str]   # "submit_raises_after_accept" | "submit_minus1" | "trades_hang" | "disconnect_next_call"
    # 收盘后没有成交完的单：状态停在 50/55，不自动变成撤单（9/4 实测行为）

class SimQMTGateway:   # 方法和 XtQMTGateway 中 oms_agent 用到的部分一致
    connect, close, account_snapshot, quotes, day_orders, day_trades, submit_limit, cancel_order
```

- [ ] 测试，每条都要完整写出：
  1. 开盘竞价按开盘价成交；
  2. 限价低于开盘价时，只有允许盘中挂单才会在 14:55 按限价成交；
  3. 收盘竞价按收盘价成交；
  4. 收盘后未成交的单状态仍是 50；
  5. 撤单先 51，推进时钟后变 54；
  6. T+1：当天买的不能当天卖；
  7. 备注截断到 24 字符；
  8. 换日后查不到前一天的委托；
  9. `submit_raises_after_accept`：交易所里有这笔单，但调用方收到异常；
  10. 容量限制造成部分成交（55 → 收盘后停在 55）。
- [ ] 实现 → 通过 → 提交 `test(agent): add QMT-faithful simulated exchange`

### 任务 14：执行代理

**文件：** 新建 `live_client/oms_agent.py`；修改 `live_client/http_client.py`；测试 `live_client/tests/test_oms_agent.py`

```python
# http_client.LiveServerClient 新增：
def post_oms_snapshot(self, payload: dict) -> dict          # POST /oms/live/snapshot
def get_oms_plan(self, account_alias, trade_date, phase) -> dict   # GET /oms/live/plan
def post_oms_events(self, account_alias, events) -> dict    # POST /oms/live/events

class OmsAgent:
    def __init__(self, cfg: LiveClientConfig, gateway, server, journal: OmsJournal, clock: Callable[[], datetime]): ...
    def snapshot(self, kind: str) -> dict                     # 账户、当日委托、成交明细（可能为 None）、9 只 ETF 报价 → 上传；服务器不可达时存入 outbox
    def fetch_plan(self, trade_date: str, phase: str) -> dict | None   # 拉取并缓存；服务器不可达时用缓存
    def execute(self, trade_date: str, phase: str, dry_run: bool) -> dict
        # 以下任一情况拒绝执行：本地 HOLD、计划 executable=False、计划日期不是今天、当前时间不在本时段窗口内
        #   （SELL：14:57:00–14:59:30；BUY：09:15:00–09:19:30）。
        # 逐笔执行：
        #   意图状态已经是 SUBMITTING/ACKED/UNKNOWN → 跳过（绝不重发）；
        #   先在当日委托里按备注查找，找到就直接 mark ACKED；
        #   BUY 额外检查：实时持仓 + 本单数量 ≤ frozen_target，否则缩量到上限；本单金额×1.001 ≤ 券商可用现金，否则按整手缩量；
        #   mark SUBMITTING → submit_limit → mark ACKED / REJECTED / UNKNOWN；dry_run 时只记录、不调用 submit。
        # 最后上传 outbox。
    def intraday(self, trade_date) -> dict                    # 每分钟任务：看板人工指令，再 snapshot("ADHOC")（10/7 取代 poll）
    def cancel_open(self) -> dict                             # 只撤本周期的 BUY 挂单（备注前缀 H），14:57 之后拒绝
    def eod(self) -> dict                                     # snapshot("EOD")，然后上传 outbox

def main(argv=None) -> int:   # python -m live_client.oms_agent {pre,sell,buy,cancel,eod,status} --date YYYYMMDD [--dry-run]
    # 用 execution_queue.account_submission_lock 取账户锁；cfg = LiveClientConfig.from_env(); cfg.validate_startup()
```

- [ ] 测试（`SimQMTGateway` 加一个基于内存字典的假服务器），全部写完整：
  1. 卖出窗口外拒绝执行；
  2. 14:57:05 卖出 → 15:00 成交 → EOD 快照里能看到；
  3. `submit_raises_after_accept` → 意图为 UNKNOWN，再次执行不会重发，EOD 时按备注找回并变为 ACKED/FILLED；
  4. 服务器不可达 → 用缓存计划执行，事件留在 outbox，恢复后补传；
  5. 本地 HOLD → 拒绝执行；
  6. 买单逐笔上限检查：在仿真交易所预先放入"晚到成交"的持仓 → 自动缩量；
  7. dry_run 不调用 submit；
  8. 14:55 撤单只撤 BUY、不碰备注不是本系统格式的单。
- [ ] 实现 → 通过 → 提交 `feat(agent): add OMS session agent`

### 任务 15：系统级回放与故障注入

**文件：** 新建 `tests/integration/test_oms_system_replay.py`（`v2.3/server` 下）、`scripts/oms_system_replay.py`

- **驱动**：真实的 `create_app`（临时数据库，开关打开）加上 `TestClient`；`OmsAgent` 的 server 换成基于 TestClient 的适配器；网关用 `SimQMTGateway`。
  - 每个信号调用 `CycleService.publish_target` 和 `approve`；
  - 按时段日程推进时钟：pre → sell/buy → intraday → cancel → eod。
- **对照组**：同一份合成数据跑 `policy_replay.run(C3, touch=True)`。
- **断言**：每个周期结束时，系统的持仓、策略现金（误差 ≤ 1 元，手续费模型统一取 1bp、最低 5 元：测试里把 SettlementService 的佣金参数设成与回测相同，印花税设为 0）与回测逐周期相等。
- **故障注入用例**，每个都要断言"没有重复下单"（仿真交易所里同一备注最多一笔单）和"账本与仿真交易所持仓一致"：
  - 服务器在 sell 时段宕机；
  - 代理在 SUBMITTING 之后崩溃再重启；
  - 日终快照缺失 → 次日开盘前对账 HELD → `oms_resolve` 放行后继续；
  - 同一批事件重复上传；
  - 成交明细查询一直不返回；
  - 下单结果不明。
- `scripts/oms_system_replay.py`：在服务器的研究目录里，用真实的 `load_inputs()` 跑最近 22 个周期，输出与 `summary3.json` 里 C3 逐周期对比的 JSON。要求执行欠配和持仓完全一致，年化收益差 ≤0.001 个百分点（来源只可能是手续费四舍五入）。

- [ ] 先写测试 → 失败 → 调试整合（预计需要回头修前面任务里的边界问题）→ 全部通过 → 在服务器上跑脚本，结果保存到 `reports/hydra_policy_20261002/system_replay.json` → 提交 `test(oms): end-to-end system replay equals research replay; fault injection`

### 任务 16：Windows 任务与休市检查

**文件：** 新建 `live_client/windows/Register-HydraOmsTasks.ps1`、`live_client/oms_holiday_check.py`；修改 `live_client/windows/Run-HydraLive.ps1`，允许 `-Module live_client.oms_agent`

- **任务表**（周一到周五；不是交易日时，代理自己查日历，直接退出）：

| 任务名 | 时间 | 命令 |
|---|---|---|
| Hydra-Oms-Pre-0900 | 09:00 | pre |
| Hydra-Oms-Buy-0914 | 09:14 | buy（代理等到 09:15:05） |
| Hydra-Oms-Cancel-1455 | 14:55 | cancel |
| Hydra-Oms-Pre-1445 | 14:45 | pre |
| Hydra-Oms-Sell-1456 | 14:56 | sell（等到 14:57:05） |
| Hydra-Oms-Eod-1505 | 15:05 | eod |
| Hydra-Oms-Eod-1530 | 15:30 | eod |
| Hydra-Oms-Intraday | 09:15–15:00 每分钟 | intraday（10/7 补充：人工指令 + 状态快照；周期步骤等锁，本任务跳过） |

- 注册前先调用 `Inspect-HydraTasks.ps1` 留档；然后停用所有旧的 `Hydra-Live-*` 任务（不删除，便于回滚）。
- `oms_holiday_check.py --account live|sim`：只读快照，结果写到 `C:\private\hydra-october\holiday-check-<account>.json`。加 `--reject-probe` 且账户为模拟账户时，发一笔休市时必然被拒的单，记录三项：返回值、委托清单里委托备注保存了几个字符、查询能不能查到之前的委托。实盘账户禁止使用 `--reject-probe`。
- [ ] 本地用 PowerShell 解析检查：

```bash
powershell -NoProfile -Command "[System.Management.Automation.Language.Parser]::ParseFile('v2.3/live_client/windows/Register-HydraOmsTasks.ps1',[ref]$null,[ref]$null) | Out-Null; 'ok'"
```

- [ ] 提交 `feat(agent): add OMS task registration and holiday checks`

### 任务 17：预发布演练（服务器，需用户确认后执行）

- 先找用户确认。
- 在 `/opt/qmt-server/releases/oms-<sha>/` 下导出代码；把生产数据库复制成 `staging.db`；用 systemd-run 起一个临时服务：端口 18001，`QMT_DB_URL` 指向 staging.db，`QMT_OMS_LIVE_ENABLED=true`。
- 在服务器上用 `SimQMTGateway` 跑执行代理，走完 9 月数据的一个周期，以及（拿到 9/30 输入后）十月计划的 dry-run。
- 结束后停掉临时服务；生产服务不重启、不改动。

### 任务 18：上线与 10/8 当天把关（每一步都要用户确认）

- 10/7：备份生产数据库 → 部署代码（开关关闭）→ 发布 9/30 目标 → 把清单交用户批准 → `approve`。
- 10/8：
  - 09:00 两个账户做快照和对账；
  - 09:15 模拟账户跑新代理的完整小周期（另起一个指向模拟账户的配置）；
  - 09:31 实盘金丝雀：一笔远低于市价的买单，回读委托备注和状态后撤掉；
  - 全部通过并经用户确认 → 打开开关 → 14:57 卖出。

---

## 自查记录

- **对设计的覆盖**：
  - 第 3 节执行规则 → 任务 1、2；
  - 4.2 数据表 → 任务 4；
  - 4.3 状态机 → 任务 3、6；
  - 4.4 和 4.4.1 时段与跨天 → 任务 6、7、14；
  - 4.5 对账闸门 → 任务 7、10；
  - 4.6 新目标与旧周期 → 任务 7（有 OPEN 周期时拒绝发布）；
  - 4.7 资金 → 任务 7；
  - 4.8 失败处理 → 任务 13、14、15；
  - 4.9 口径一致 → 任务 2、15；
  - 第 5 节危险入口 → 任务 9；
  - 第 7、8 节 → 任务 15–18；
  - 4.10 紧急通道 → 本期不做（用户同意）。
- **和设计不同的地方（已在计划开头注明）**：持仓变化归因改为人工确认（任务 10 的 `oms_resolve.py`）；不做开盘前重算。
- **名称一致性**：`OrderState`、`classify_qmt`、`legacy_status`、`plan_for`、`ingest_snapshot`、`submit_limit`、`day_orders`、`day_trades`，在各任务里写法一致。
