# V713 Stock Leg and Synthetic Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 20 万元 V713 股票腿能按每月 V713 篮子发布真实目标（AUX 时清仓），并每天生成只读的 V713 合成净值（40 万基准，0 / 20 / 50bp 三条线）。

**Architecture:** 股票腿复用已校验的 V713 篮子和现有 OMS 周期，只新增“取股票部分并放大”的派生和一种显式清仓目标；每月状态写入一张只追加的记录表。合成账本是一个纯计算引擎，读两条腿的 `PerfSnapshot` 单位净值和状态记录，按设计文档第六节的时点规则持有两腿份额，结果写成 JSON 文件，由只读接口和静态看板页展示。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy（SQLite）、pandas、pytest。

设计依据：[V713 股票腿实盘与合成验证方案](V713_STOCK_LEG_SYNTHETIC_DESIGN_20261023.md)。本计划实现设计第八节的 E1、E2、E5、E6、E7、E8。

---

## 实施记录（2026-10-25）

本计划已在分支 `feat/v713-stock-leg` 上实施。与下文任务的差异：

1. **进入 AUX 的条件。**设计和 10/25 确认记录要求“股票市值 ≤ 1% **且**没有未终局卖出委托”，下文 Task 7 只检查了 1%。实现中 `run_synthetic` 增加 `open_sell_dates`，`load_inputs` 从股票腿的卖单推出：卖单只有在现在已终局、且终局时间不晚于当天结束时，才算当天已完成。因此日后重跑不会把切换提前。
2. **运维密钥路径白名单。**`app/auth.py` 的 `OPERATOR_PATHS` 只允许列出的路径，下文 Task 10 没有写这一步；实现中加入 `/oms/live/v713/synthetic`，live 客户端密钥仍为 403。
3. **新增冻结规则模板。**`plugins/v713/stockleg_policy_template_v1.json` 按 10/25 确认的费率与 Hydra 流程填写，对照说明见 [V713_STOCKLEG_EXECUTION_RULES_20261025.md](V713_STOCKLEG_EXECUTION_RULES_20261025.md)。
4. **测试数。**`test_oms_stock_leg.py` 13 项、`test_v713_synthetic.py` 15 项、`test_v713_synthetic_api.py` 3 项。全量单元测试 907 项通过，另有 76 项失败，与改动前完全相同，都是 Windows 测试环境问题（缺 `fcntl`、GBK 解码）。
5. **Linux 全量测试（2026-10-26）。**在 qmt 隔离目录（`research-runs/v713-linux-tests-20261026-01`，用生产 venv，测试依赖另装在隔离目录）运行服务器单元测试和客户端测试：本分支 1253 项、PR #3 基线 1214 项全部通过。Windows 上失败的 76 项和 2 项客户端测试在 Linux 上都通过。
6. **远程核对。**qmt 隔离目录 `research-runs/v713-stock-leg-checks-20261025-01` 用本分支代码复现 10/16 结果，误差 ≤ 8e-16；用当期生产篮子的副本导出股票腿，得到 50 只、各 2%。汇总见 [checks.json](../reports/v713_stock_leg_checks_20261025/checks.json)。

## 范围与前提

| 设计项 | 本计划 | 说明 |
|---|---|---|
| E0 共享账户基础设施验收 | 不含 | 按 [Handoff](V713_SHARED_OMS_HANDOFF_20261015.md) N01–N03 执行；本计划代码基于 PR #3 分支加 `1a0bc81` |
| E1 股票腿目标适配器 | Task 1、3、4、5 | 含每月状态记录和证券规格展开 |
| E2 清仓目标 | Task 2 | |
| E3 执行政策与回放 | 不含 | 研究任务，另立计划；产出 `V713_STOCKLEG_v1` 的模板参数 |
| E4 实例与期初 | 不含 | 运维操作，用现有 `/accounts/initialize-from-qmt`、`/accounts/capital-transfers`；见文末“上线操作” |
| E5 单位净值 | Task 6、9 | `PerfSnapshot.daily_return` 已按 `CashFlowJournal` 剔除资金进出，直接连乘即可 |
| E6 合成账本 | Task 6、7、8、9 | |
| E7 看板 | Task 10 | |
| E8 测试与回放 | 每个 Task 自带测试；Task 8 对拍 10/16 引擎；Task 11 全量回归与远程回放 | |

与设计文档的两处落地差异（Task 11 同步修改设计文档）：

1. 设计写“结果写入 `ParquetStore`”。`ParquetStore` 的类别固定为 `stocks / indexes / etfs`，因此改为写 JSON 文件并原子替换。
2. OMS 目标版本表不保存 sleeve，而 TOP50 与 T1 的股票腿目标完全相同。因此新增 `v713_state_records` 表，每月记录一次状态，供合成账本读取。

所有命令在 `v2.3/server` 目录下执行，测试环境变量：

```bash
export PYTHONDONTWRITEBYTECODE=1 QMT_SCHEDULER_ENABLED=false
```

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `app/oms/v713_stock_leg.py` | 新建 | 证券规则展开、股票腿派生、每月状态记录 |
| `app/oms/models.py` | 修改 | 新增 `V713StateRecord` |
| `app/oms/cycles.py` | 修改 | `publish_target` 增加 `liquidate` |
| `scripts/oms_ops.py` | 修改 | 新增 `publish-v713-stock-leg` 子命令 |
| `app/services/v713_synthetic.py` | 新建 | 合成引擎（纯函数）与数据库读取 |
| `scripts/run_v713_synthetic.py` | 新建 | 每日任务：读输入、跑三条线、原子写 JSON |
| `plugins/v713/synthetic_policy_v1.json` | 新建 | 冻结的合成规则 |
| `app/settings.py` | 修改 | `v713_synthetic_output` |
| `app/oms/api.py` | 修改 | `GET /oms/live/v713/synthetic` |
| `app/oms/dashboard.py` | 修改 | `GET /dashboard/v713-synthetic` |
| `app/web/v713/index.html` | 新建 | 只读看板页 |
| `tests/unit/test_oms_stock_leg.py` | 新建 | Task 1–5 |
| `tests/unit/test_v713_synthetic.py` | 新建 | Task 6–9 |
| `tests/unit/test_v713_synthetic_api.py` | 新建 | Task 10 |

---

### Task 1: 证券规格按规则展开

V713 policy 要求篮子与持仓里每个证券都有完整的显式规格。股票腿每月换名单，手写不可行；本任务把“按板块的三条规则”展开成显式规格，再交给现有 `validate_v713_policy` 校验。

**Files:**
- Create: `app/oms/v713_stock_leg.py`
- Test: `tests/unit/test_oms_stock_leg.py`

- [ ] **Step 1: Write the failing test**

```python
"""V713 stock leg: rule expansion, derived targets, liquidation and monthly state."""
import json

import pandas as pd
import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState
from app.oms.cycles import CycleConflict, CycleService
from app.oms.instruments import InstrumentSpec, instrument_to_dict
from app.oms.ledger import OrderLedger
from app.oms.models import OmsCycle, OmsOrder
from app.oms.planner import Policy, policy_to_dict
from app.oms.v713 import build_publication, validate_v713_policy
from app.oms.v713_stock_leg import expand_policy, instrument_rule
from app.services.reconcile import ReconcileService
from app.services.settlement import SettlementService
from plugins.v713_relay import basket_hash

CAL = ["20260831", "20260901", "20260929", "20260930", "20261008", "20261009", "20261012",
       "20261013", "20261014", "20261015", "20261016", "20261019", "20261020", "20261021"]
NOW = "2026-10-07T20:00:00+08:00"
ALIAS = "shared"
INSTANCE = "live_v713_stockleg"
CLOSES = {"600001.SH": 10.0, "600002.SH": 20.0, "511260.SH": 120.0}


def _template():
    board = instrument_to_dict(InstrumentSpec("stock", .01, commission_rate=.0002, stamp_duty_sell=.0005))
    star = instrument_to_dict(InstrumentSpec("stock", .01, buy_lot=1, min_buy_quantity=200, sell_lot=1,
                                             commission_rate=.0002, stamp_duty_sell=.0005))
    etf = instrument_to_dict(InstrumentSpec("etf", .001, commission_rate=.0002))
    base = policy_to_dict(Policy(policy_id="V713_STOCKLEG_TEST", strategy_id="v713", require_instrument_specs=True,
                                 reserve=.01, buy_bps=()))
    base.pop("instruments")
    return base | {"instrument_rules": {"stock_board_lot": board, "stock_star": star, "etf": etf}}


def test_instrument_rules_cover_board_lot_star_and_etf():
    assert instrument_rule("600001.SH") == "stock_board_lot"
    assert instrument_rule("000001.SZ") == "stock_board_lot"
    assert instrument_rule("300750.SZ") == "stock_board_lot"
    assert instrument_rule("688981.SH") == "stock_star"
    assert instrument_rule("511260.SH") == "etf"
    assert instrument_rule("159915.SZ") == "etf"
    with pytest.raises(ValueError, match="830799.BJ"):
        instrument_rule("830799.BJ")


def test_expand_policy_gives_every_symbol_an_explicit_spec():
    symbols = ["600001.SH", "688001.SH", "511260.SH"]
    payload = expand_policy(_template(), symbols)
    assert "instrument_rules" not in payload
    assert payload["instruments"]["688001.SH"]["min_buy_quantity"] == 200
    assert payload["instruments"]["511260.SH"]["asset_type"] == "etf"
    validate_v713_policy(payload, symbols)


def test_expand_policy_requires_exactly_the_three_rules():
    template = _template()
    del template["instrument_rules"]["etf"]
    with pytest.raises(ValueError, match="exactly"):
        expand_policy(template, ["600001.SH"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_oms_stock_leg.py -q -p no:cacheprovider`
Expected: collection error `ModuleNotFoundError: No module named 'app.oms.v713_stock_leg'`

- [ ] **Step 3: Write minimal implementation**

`app/oms/v713_stock_leg.py`：

```python
"""V713 stock leg: derive a pure-stock target from the validated monthly V713 basket.

Design: docs/V713_STOCK_LEG_SYNTHETIC_DESIGN_20261023.md section 5. The stock leg holds
the basket's stock names, rescaled to the whole leg, in TOP50 and T1, and nothing in
AUX. It never recomputes TOP50, Hydra or the state signal.
"""
from __future__ import annotations

import copy

_RULES = ("stock_board_lot", "stock_star", "etf")


def instrument_rule(symbol: str) -> str:
    """Frozen instrument rule for a symbol: 100-share board lots, STAR (688) or an ETF."""
    code, _, market = symbol.partition(".")
    if market == "SH" and code.startswith("688"):
        return "stock_star"
    if (market == "SH" and code.startswith("60")) or (market == "SZ" and code[:2] in ("00", "30")):
        return "stock_board_lot"
    if (market == "SH" and code.startswith("5")) or (market == "SZ" and code[:2] in ("15", "16")):
        return "etf"
    raise ValueError(f"no instrument rule for {symbol}")


def expand_policy(template: dict, symbols) -> dict:
    """Explicit V713 policy payload in which every symbol carries its rule's full spec."""
    if set(template.get("instrument_rules", {})) != set(_RULES):
        raise ValueError(f"policy template must define exactly {sorted(_RULES)}")
    payload = copy.deepcopy({key: value for key, value in template.items() if key != "instrument_rules"})
    payload["instruments"] = {symbol: dict(template["instrument_rules"][instrument_rule(symbol)])
                              for symbol in sorted(set(symbols))}
    return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_oms_stock_leg.py -q -p no:cacheprovider`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add app/oms/v713_stock_leg.py tests/unit/test_oms_stock_leg.py
git commit -m "feat(v713): expand stock-leg instrument rules into an explicit policy"
```

---

### Task 2: 显式清仓目标

`publish_target` 现在要求权重非空且合计为 1，AUX 月份无法表达“全部卖出”。增加 `liquidate=True`：只接受空权重，所有持仓冻结为零目标，沿用现有退出与残余逻辑。

**Files:**
- Modify: `app/oms/cycles.py:66-74`（签名与权重校验）、`app/oms/cycles.py:98`（持仓读取之后）
- Test: `tests/unit/test_oms_stock_leg.py`

- [ ] **Step 1: Write the failing test**

追加到 `tests/unit/test_oms_stock_leg.py`：

```python
def _policy():
    return expand_policy(_template(), ["600001.SH", "600002.SH", "511260.SH"])


def _service(tmp_path, positions=None, cash=200000.0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = make_engine(f"sqlite:///{tmp_path}/oms.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(InstanceState(instance_id=INSTANCE, execution_domain="live", account_alias=ALIAS,
                            ledger_mode="attributed", virtual_cash=cash, virtual_positions=dict(positions or {}),
                            owned_symbols=sorted(positions or {}), last_update="x"))
        s.commit()
    settlement = SettlementService(sf, commission_rate=.0002, min_commission=5.0, stamp_duty_sell=.0005)
    return sf, CycleService(sf, OrderLedger(sf, settlement), ReconcileService(sf))


def _liquidate(svc, **changes):
    return svc.publish_target(**({"instance_id": INSTANCE, "account_alias": ALIAS, "signal_date": "20260930",
                                  "weights": {}, "signal_closes": CLOSES, "calendar": CAL,
                                  "source_sha256": "a" * 64, "now": NOW, "policy": _policy(),
                                  "earliest_execution_date": "20261008", "liquidate": True} | changes))


def test_liquidation_target_sells_every_holding(tmp_path):
    sf, svc = _service(tmp_path, positions={"600001.SH": 1000, "600002.SH": 500}, cash=5000.0)
    out = _liquidate(svc)
    assert {row["symbol"]: row["target"] for row in out["shares"]} == {"600001.SH": 0, "600002.SH": 0}
    with sf() as s:
        orders = s.query(OmsOrder).order_by(OmsOrder.symbol).all()
        assert [(o.symbol, o.side) for o in orders] == [("600001.SH", "SELL"), ("600002.SH", "SELL")]


def test_liquidation_target_rejects_weights_and_an_instance_without_stock(tmp_path):
    _, svc = _service(tmp_path / "held", positions={"600001.SH": 1000})
    with pytest.raises(ValueError, match="carries no weights"):
        _liquidate(svc, weights={"600001.SH": 1.0})
    _, svc = _service(tmp_path / "cash")
    with pytest.raises(ValueError, match="nothing to liquidate"):
        _liquidate(svc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_oms_stock_leg.py -q -p no:cacheprovider -k liquidation`
Expected: FAIL with `TypeError: CycleService.publish_target() got an unexpected keyword argument 'liquidate'`

- [ ] **Step 3: Write minimal implementation**

`app/oms/cycles.py` 中把签名和开头的权重校验（第 66–74 行）替换为：

```python
    def publish_target(self, *, instance_id: str, account_alias: str, signal_date: str, weights: dict,
                       signal_closes: dict, calendar: list, source_sha256: str, now: str,
                       policy: Policy | dict | None = None, metadata: dict | None = None,
                       provenance: dict | None = None, earliest_execution_date: str | None = None,
                       liquidate: bool = False) -> dict:
        policy = policy_from_dict(policy) if isinstance(policy, dict) else (policy or self.policy)
        if liquidate:
            # An explicit all-cash target (V713 AUX): every held symbol is frozen at zero below.
            if weights:
                raise ValueError("a liquidation target carries no weights")
        else:
            if not weights or any(not math.isfinite(float(w)) or float(w) < 0 for w in weights.values()):
                raise ValueError("target weights must be finite, non-negative and nonempty")
            if abs(sum(float(w) for w in weights.values()) - 1.0) > 1e-6:
                raise ValueError("target weights must sum to one")
```

在 `positions = {s: int(q) ...}`（原第 98 行）之后紧接着加：

```python
            if liquidate and not positions:
                raise ValueError("nothing to liquidate: the instance holds no position")
```

其余流程不变：`lot_target(nav, {}, ...)` 返回空字典，随后 `frozen.setdefault(symbol, 0)` 把每个持仓设为零目标。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_oms_stock_leg.py tests/unit/test_oms_cycles.py tests/unit/test_oms_v713.py -q -p no:cacheprovider`
Expected: 全部通过（新增 2 项，原有 cycles / v713 测试不变）

- [ ] **Step 5: Commit**

```bash
git add app/oms/cycles.py tests/unit/test_oms_stock_leg.py
git commit -m "feat(oms): explicit liquidation target for the V713 AUX state"
```

---

### Task 3: 从 V713 篮子派生股票腿目标

**Files:**
- Modify: `app/oms/v713_stock_leg.py`
- Test: `tests/unit/test_oms_stock_leg.py`

- [ ] **Step 1: Write the failing test**

追加 import：`from app.oms.v713_stock_leg import STATE_STOCK_WEIGHT, derive_stock_leg`，并追加：

```python
def _basket(tmp_path, rows, sleeve, *, decision="20261008", as_of="20260930"):
    frame = pd.DataFrame([{"code": code, "weight": weight, "strategy_version": "v7.13-base", "sleeve": sleeve,
                           "decision_date": decision, "as_of_date": as_of} for code, weight in rows])
    frame["basket_sha256"] = basket_hash(frame)
    path = tmp_path / f"{sleeve}.parquet"
    frame.to_parquet(path, index=False)
    return path


def _base(path, held=()):
    return build_publication(path, closes=CLOSES, calendar=CAL, now=NOW, held_symbols=list(held),
                             policy_payload=_policy())


def test_t1_basket_rescales_stock_names_to_the_whole_leg(tmp_path):
    path = _basket(tmp_path, [("600001.SH", .25), ("600002.SH", .25), ("511260.SH", .5)], "T1_5050")
    leg = derive_stock_leg(_base(path))
    assert leg["weights"] == {"600001.SH": .5, "600002.SH": .5}
    assert leg["liquidate"] is False
    assert leg["provenance"]["sleeve"] == "T1_5050"
    assert leg["provenance"]["transform"] == "v713-stock-leg-v1"
    assert leg["provenance"]["source_allocation_sha256"] != leg["source_sha256"]


def test_top50_basket_is_unchanged_and_aux_basket_liquidates(tmp_path):
    top = derive_stock_leg(_base(_basket(tmp_path, [("600001.SH", .6), ("600002.SH", .4)], "TOP50")))
    assert top["weights"] == {"600001.SH": .6, "600002.SH": .4}
    aux = derive_stock_leg(_base(_basket(tmp_path, [("511260.SH", 1.)], "AUX_HYDRA"), held=["600001.SH"]))
    assert aux["weights"] == {} and aux["liquidate"] is True


def test_stock_weight_must_match_the_sleeve(tmp_path):
    assert STATE_STOCK_WEIGHT == {"TOP50": 1.0, "T1_5050": .5, "AUX_HYDRA": 0.0}
    path = _basket(tmp_path, [("600001.SH", .7), ("511260.SH", .3)], "T1_5050")
    with pytest.raises(ValueError, match="expected 0.5"):
        derive_stock_leg(_base(path))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_oms_stock_leg.py -q -p no:cacheprovider`
Expected: collection error `ImportError: cannot import name 'STATE_STOCK_WEIGHT'`

- [ ] **Step 3: Write minimal implementation**

在 `app/oms/v713_stock_leg.py` 的 `import copy` 之后加 `import hashlib`、`import json`，在 `_RULES` 之前加常量，文件末尾加函数：

```python
STATE_STOCK_WEIGHT = {"TOP50": 1.0, "T1_5050": 0.5, "AUX_HYDRA": 0.0}
STOCK_LEG_TRANSFORM = "v713-stock-leg-v1"
```

```python
def _sha(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def derive_stock_leg(payload: dict) -> dict:
    """Publication kwargs for the stock leg from ``app.oms.v713.build_publication`` output.

    TOP50 and T1 hold the basket's stock names rescaled to the whole leg; AUX is an
    explicit liquidation. The identity covers the transform, so a stock-leg target can
    never be mistaken for the mixed V713 allocation it came from.
    """
    from app.oms.planner import get_instrument

    policy, sleeve = payload["policy"], payload["provenance"]["sleeve"]
    if sleeve not in STATE_STOCK_WEIGHT:
        raise ValueError(f"unknown V713 sleeve {sleeve}")
    stocks = {symbol: float(weight) for symbol, weight in sorted(payload["weights"].items())
              if weight > 0 and get_instrument(symbol, policy).asset_type == "stock"}
    total = sum(stocks.values())
    if abs(total - STATE_STOCK_WEIGHT[sleeve]) > 1e-4:
        raise ValueError(f"{sleeve} basket carries stock weight {total:.6f}, expected {STATE_STOCK_WEIGHT[sleeve]}")
    liquidate = sleeve == "AUX_HYDRA"
    weights = {} if liquidate else {symbol: weight / total for symbol, weight in stocks.items()}
    identity = _sha({"transform": STOCK_LEG_TRANSFORM, "sleeve": sleeve, "signal_date": payload["signal_date"],
                     "weights": weights, "source_allocation_sha256": payload["source_sha256"]})
    provenance = {**payload["provenance"], "transform": STOCK_LEG_TRANSFORM,
                  "source_allocation_sha256": payload["source_sha256"],
                  "stock_leg_allocation_sha256": identity, "liquidate": liquidate}
    return {**payload, "weights": weights, "source_sha256": identity, "provenance": provenance,
            "liquidate": liquidate}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_oms_stock_leg.py -q -p no:cacheprovider`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add app/oms/v713_stock_leg.py tests/unit/test_oms_stock_leg.py
git commit -m "feat(v713): derive the stock-leg target from the monthly V713 basket"
```

---

### Task 4: 每月状态记录

**Files:**
- Modify: `app/oms/models.py`（`__all__` 与文件末尾）
- Modify: `app/oms/v713_stock_leg.py`
- Test: `tests/unit/test_oms_stock_leg.py`

- [ ] **Step 1: Write the failing test**

追加 import：`from app.oms.models import V713StateRecord`、`from app.oms.v713_stock_leg import record_state`，并追加：

```python
def test_state_record_is_idempotent_and_frozen(tmp_path):
    sf, _ = _service(tmp_path)
    leg = derive_stock_leg(_base(_basket(tmp_path, [("600001.SH", 1.)], "TOP50")))
    assert record_state(sf, instance_id=INSTANCE, payload=leg, target_version_id=None, now=NOW)["sleeve"] == "TOP50"
    for _ in range(2):
        out = record_state(sf, instance_id=INSTANCE, payload=leg, target_version_id="tv_1", now=NOW)
        assert out["target_version_id"] == "tv_1"
    with pytest.raises(CycleConflict, match="another target version"):
        record_state(sf, instance_id=INSTANCE, payload=leg, target_version_id="tv_2", now=NOW)
    other = derive_stock_leg(_base(_basket(tmp_path, [("600001.SH", .5), ("511260.SH", .5)], "T1_5050")))
    with pytest.raises(CycleConflict, match="different allocation"):
        record_state(sf, instance_id=INSTANCE, payload=other, target_version_id=None, now=NOW)
    with sf() as s:
        row = s.get(V713StateRecord, (INSTANCE, "20260930"))
        assert (row.sleeve, row.decision_date, row.target_version_id) == ("TOP50", "20261008", "tv_1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_oms_stock_leg.py -q -p no:cacheprovider`
Expected: collection error `ImportError: cannot import name 'V713StateRecord'`

- [ ] **Step 3: Write minimal implementation**

`app/oms/models.py`：在 `__all__` 列表末尾加 `"V713StateRecord"`，文件末尾加：

```python
class V713StateRecord(Base):
    """Monthly V713 state as published to the stock leg; the synthetic ledger reads it."""
    __tablename__ = "v713_state_records"
    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    signal_date: Mapped[str] = mapped_column(String, primary_key=True)
    sleeve: Mapped[str] = mapped_column(String, nullable=False)
    decision_date: Mapped[str] = mapped_column(String, nullable=False)
    basket_sha256: Mapped[str] = mapped_column(String, nullable=False)
    allocation_sha256: Mapped[str] = mapped_column(String, nullable=False)
    target_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
```

`init_db` 使用 `create_all`，新表在现有数据库上也会创建，旧表不变。

`app/oms/v713_stock_leg.py` 末尾加：

```python
def record_state(session_factory, *, instance_id: str, payload: dict, target_version_id: str | None,
                 now: str) -> dict:
    """Record the month's V713 state for the synthetic ledger; idempotent and never rewritten."""
    from app.oms.cycles import CycleConflict
    from app.oms.models import V713StateRecord

    provenance = payload["provenance"]
    with session_factory() as session:
        row = session.get(V713StateRecord, (instance_id, payload["signal_date"]))
        if row is None:
            row = V713StateRecord(instance_id=instance_id, signal_date=payload["signal_date"],
                                  sleeve=provenance["sleeve"], decision_date=provenance["decision_date"],
                                  basket_sha256=provenance["basket_sha256"],
                                  allocation_sha256=payload["source_sha256"],
                                  target_version_id=target_version_id, recorded_at=now)
            session.add(row)
        elif (row.sleeve, row.allocation_sha256) != (provenance["sleeve"], payload["source_sha256"]):
            raise CycleConflict("V713 state for this month is already recorded with a different allocation")
        elif target_version_id is not None and row.target_version_id not in (None, target_version_id):
            raise CycleConflict("V713 state for this month already points to another target version")
        elif target_version_id is not None:
            row.target_version_id = target_version_id
        session.commit()
        return {"instance_id": row.instance_id, "signal_date": row.signal_date, "sleeve": row.sleeve,
                "target_version_id": row.target_version_id}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_oms_stock_leg.py tests/unit/test_oms_models.py -q -p no:cacheprovider`
Expected: 全部通过

- [ ] **Step 5: Commit**

```bash
git add app/oms/models.py app/oms/v713_stock_leg.py tests/unit/test_oms_stock_leg.py
git commit -m "feat(v713): record each month's V713 state for the synthetic ledger"
```

---

### Task 5: `publish-v713-stock-leg` 运维命令

**Files:**
- Modify: `scripts/oms_ops.py`（docstring、parser、publish 分支）
- Test: `tests/unit/test_oms_stock_leg.py`

- [ ] **Step 1: Write the failing test**

追加 import：`from app.settings import Settings`、`from scripts import oms_ops`，并追加：

```python
def _cli_args(tmp_path, basket):
    args = ["publish-v713-stock-leg", "--instance", INSTANCE, "--account", ALIAS, "--basket", str(basket),
            "--now", NOW]
    for name, value in {"policy-template": _template(), "closes": CLOSES, "calendar": CAL}.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value))
        args += [f"--{name}", str(path)]
    return args


def _settings(tmp_path):
    return Settings(_env_file=None, db_url=f"sqlite:///{tmp_path}/oms.db")


def test_cli_t1_month_publishes_rescaled_target_and_records_state(tmp_path, capsys):
    sf, _ = _service(tmp_path)
    basket = _basket(tmp_path, [("600001.SH", .25), ("600002.SH", .25), ("511260.SH", .5)], "T1_5050")
    args = _cli_args(tmp_path, basket)
    assert oms_ops.main(args, session_factory=sf, settings=_settings(tmp_path)) == 0
    assert "DRY RUN" in capsys.readouterr().out
    with sf() as s:
        assert s.query(OmsCycle).count() == 0 and s.query(V713StateRecord).count() == 0
    for _ in range(2):
        assert oms_ops.main(args + ["--apply"], session_factory=sf, settings=_settings(tmp_path)) == 0
    with sf() as s:
        cycle = s.query(OmsCycle).one()
        assert set(cycle.frozen_target) == {"600001.SH", "600002.SH"}
        state = s.query(V713StateRecord).one()
        assert state.sleeve == "T1_5050" and state.target_version_id == cycle.target_version_id


def test_cli_aux_month_liquidates_held_stock(tmp_path):
    sf, _ = _service(tmp_path, positions={"600001.SH": 1000}, cash=1000.0)
    basket = _basket(tmp_path, [("511260.SH", 1.)], "AUX_HYDRA")
    assert oms_ops.main(_cli_args(tmp_path, basket) + ["--apply"], session_factory=sf,
                        settings=_settings(tmp_path)) == 0
    with sf() as s:
        assert s.query(OmsCycle).one().frozen_target == {"600001.SH": 0}
        assert s.query(V713StateRecord).one().sleeve == "AUX_HYDRA"


def test_cli_aux_month_with_cash_only_records_state(tmp_path, capsys):
    sf, _ = _service(tmp_path)
    basket = _basket(tmp_path, [("511260.SH", 1.)], "AUX_HYDRA")
    assert oms_ops.main(_cli_args(tmp_path, basket) + ["--apply"], session_factory=sf,
                        settings=_settings(tmp_path)) == 0
    assert "STATE_ONLY" in capsys.readouterr().out
    with sf() as s:
        assert s.query(OmsCycle).count() == 0
        assert s.query(V713StateRecord).one().target_version_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_oms_stock_leg.py -q -p no:cacheprovider -k cli`
Expected: FAIL with `SystemExit: 2`（argparse: invalid choice `publish-v713-stock-leg`）

- [ ] **Step 3: Write minimal implementation**

`scripts/oms_ops.py`：

(a) docstring 的命令示例里加一行：

```text
    PYTHONPATH=. python -m scripts.oms_ops publish-v713-stock-leg --instance live_v713_stockleg --account <alias> \
        --basket v713_target_latest.parquet --policy-template t.json --closes c.json --calendar cal.json [--apply]
```

(b) 在 `a = sub.add_parser("approve")` 之前加：

```python
    k = sub.add_parser("publish-v713-stock-leg",
                       help="derive the V713 stock-leg target from a monthly basket and record its state")
    for name in ("--instance", "--account", "--basket", "--policy-template", "--closes", "--calendar"):
        k.add_argument(name, required=True)
    k.add_argument("--apply", action="store_true")
    k.add_argument("--now", default=None, help="zoned ISO publication time for an isolated rehearsal")
```

(c) 把 publish 分支开头到 `else:`（原第 108–125 行）替换为：

```python
    if args.command in ("publish", "publish-v713", "publish-v713-stock-leg"):
        from app.oms.planner import policy_from_dict, policy_to_dict

        closes = json.loads(Path(args.closes).read_text(encoding="utf-8"))
        calendar = json.loads(Path(args.calendar).read_text(encoding="utf-8"))
        now = args.now or _now()
        if args.command in ("publish-v713", "publish-v713-stock-leg"):
            from app.models import InstanceState
            from app.oms.v713 import build_publication
            with session_factory() as session:
                instance = session.get(InstanceState, args.instance)
                if instance is None or instance.execution_domain != "live" or instance.account_alias != args.account:
                    raise SystemExit("V713 publication requires the initialized live instance for this account")
                held = [s for s, q in (instance.virtual_positions or {}).items() if q]
            if args.command == "publish-v713":
                policy_payload = json.loads(Path(args.policy).read_text(encoding="utf-8"))
            else:
                import pandas as pd
                from app.oms.v713_stock_leg import expand_policy
                codes = set(pd.read_parquet(args.basket, columns=["code"])["code"].astype(str))
                policy_payload = expand_policy(json.loads(Path(args.policy_template).read_text(encoding="utf-8")),
                                               codes | set(held))
            payload = build_publication(args.basket, closes=closes, calendar=calendar,
                                        policy_payload=policy_payload, now=now, held_symbols=held)
            if args.command == "publish-v713-stock-leg":
                from app.oms.v713_stock_leg import derive_stock_leg, record_state
                payload = derive_stock_leg(payload)
                if payload["liquidate"] and not held:
                    print(json.dumps({"sleeve": payload["provenance"]["sleeve"], "action": "STATE_ONLY",
                                      "reason": "the stock leg already holds no stock"}, ensure_ascii=False))
                    if not args.apply:
                        print("DRY RUN: nothing written.")
                        return 0
                    print(json.dumps(record_state(session_factory, instance_id=args.instance, payload=payload,
                                                  target_version_id=None, now=now), ensure_ascii=False))
                    return 0
        else:
```

(d) 把 apply 之后的两行（`out = service.publish_target(...)` 与其后的 `print`）替换为：

```python
        out = service.publish_target(instance_id=args.instance, account_alias=args.account, **payload)
        print(json.dumps({k: out[k] for k in ("cycle_id", "status", "target_version_id", "lot_gap")}, indent=2))
        if args.command == "publish-v713-stock-leg":
            print(json.dumps(record_state(session_factory, instance_id=args.instance, payload=payload,
                                          target_version_id=out["target_version_id"], now=now), ensure_ascii=False))
        return 0
```

重复 `--apply` 时 `publish_target` 返回已有周期（同一目标版本），`record_state` 幂等，两步都不会重复写。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_oms_stock_leg.py tests/unit/test_oms_v713.py tests/unit/test_oms_ops.py -q -p no:cacheprovider`
Expected: 全部通过

- [ ] **Step 5: Commit**

```bash
git add scripts/oms_ops.py tests/unit/test_oms_stock_leg.py
git commit -m "feat(v713): publish-v713-stock-leg operator command"
```

---

### Task 6: 合成引擎基础：单位净值、两腿调整、冻结规则

**Files:**
- Create: `app/services/v713_synthetic.py`
- Create: `plugins/v713/synthetic_policy_v1.json`
- Test: `tests/unit/test_v713_synthetic.py`

- [ ] **Step 1: Write the failing test**

```python
"""Synthetic V713 ledger over the live stock leg and live Hydra (read-only)."""
import json
from pathlib import Path

import pytest

from app.services.v713_synthetic import SyntheticPolicy, load_synthetic_policy, rebalance, unit_nav

POLICY_FILE = Path(__file__).resolve().parents[2] / "plugins" / "v713" / "synthetic_policy_v1.json"
POLICY = SyntheticPolicy(policy_id="V713_SYNTHETIC_TEST", stock_instance="live_v713_stockleg",
                         hydra_instance="live_hydra_v481_rb", label="test")


def test_unit_nav_chains_flow_adjusted_returns():
    units = unit_nav([("20261202", .1), ("20261201", None), ("20261203", -.5)])
    assert units == pytest.approx({"20261201": 1.0, "20261202": 1.1, "20261203": .55})
    with pytest.raises(ValueError, match="20261202"):
        unit_nav([("20261201", None), ("20261202", None)])


def test_rebalance_matches_the_design_example():
    # TOP50 -> T1 at 400k: virtual stock sale and virtual Hydra purchase, 20bp each.
    values, cash, cost, traded = rebalance([400_000.0, 0.0], 0.0, .5, (.002, .002))
    assert values == pytest.approx([200_000.0, 199_201.596806])
    assert cash == pytest.approx(0.0, abs=1e-9)
    assert cost == pytest.approx(798.403194)
    assert traded == pytest.approx(399_201.596806)


def test_real_leg_trades_are_not_charged_again():
    # AUX exit: the Hydra sale is virtual, the stock rebuild is real and already in its NAV.
    values, _, cost, _ = rebalance([0.0, 400_000.0], 0.0, 1.0, (0.0, .002))
    assert cost == pytest.approx(800.0)
    assert values[0] == pytest.approx(399_200.0)


def test_frozen_policy_file_loads_and_unversioned_policy_is_rejected(tmp_path):
    policy = load_synthetic_policy(POLICY_FILE)
    assert (policy.base_value, policy.cost_bps, policy.primary_bps) == (400_000.0, (0.0, 20.0, 50.0), 20.0)
    bad = json.loads(POLICY_FILE.read_text(encoding="utf-8")) | {"policy_id": "synthetic"}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="V713_SYNTHETIC_"):
        load_synthetic_policy(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_v713_synthetic.py -q -p no:cacheprovider`
Expected: collection error `ModuleNotFoundError: No module named 'app.services.v713_synthetic'`

- [ ] **Step 3: Write minimal implementation**

`plugins/v713/synthetic_policy_v1.json`：

```json
{
  "policy_id": "V713_SYNTHETIC_V1",
  "stock_instance": "live_v713_stockleg",
  "hydra_instance": "live_hydra_v481_rb",
  "label": "V713（511260 版 Hydra，20 万股票腿合成 40 万）",
  "base_value": 400000.0,
  "cost_bps": [0.0, 20.0, 50.0],
  "primary_bps": 20.0,
  "liquidated_stock_weight": 0.01,
  "review_after_sessions": 10
}
```

`app/services/v713_synthetic.py`：

```python
"""Read-only synthetic V713 ledger over two live legs.

Design: docs/V713_STOCK_LEG_SYNTHETIC_DESIGN_20261023.md section 6. It holds units of
the stock leg and of Hydra at each leg's flow-adjusted unit NAV and moves them only at
session closes. It never creates targets or orders.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path

from app.oms.v713_stock_leg import STATE_STOCK_WEIGHT

AUX = "AUX_HYDRA"


@dataclass(frozen=True)
class SyntheticPolicy:
    policy_id: str
    stock_instance: str
    hydra_instance: str
    label: str
    base_value: float = 400_000.0
    cost_bps: tuple = (0.0, 20.0, 50.0)
    primary_bps: float = 20.0
    liquidated_stock_weight: float = 0.01
    review_after_sessions: int = 10


@dataclass(frozen=True)
class MonthState:
    signal_date: str
    sleeve: str
    anchor_date: str  # the close at which this month's move applies: its first execution session


def load_synthetic_policy(path) -> SyntheticPolicy:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    policy = SyntheticPolicy(**{**payload, "cost_bps": tuple(float(b) for b in payload["cost_bps"])})
    if not policy.policy_id.startswith("V713_SYNTHETIC_"):
        raise ValueError("synthetic policy requires a versioned V713_SYNTHETIC_ policy_id")
    if policy.primary_bps not in policy.cost_bps or policy.base_value <= 0:
        raise ValueError("the primary cost must be a reported line and the base positive")
    if not 0 < policy.liquidated_stock_weight < .05 or policy.review_after_sessions < 1:
        raise ValueError("invalid liquidation completion rule")
    return policy


def unit_nav(snapshots) -> dict[str, float]:
    """Chain flow-adjusted daily returns (``PerfSnapshot.daily_return``); the first snapshot is 1."""
    units, value = {}, None
    for date, daily_return in sorted(snapshots):
        if value is None:
            value = 1.0
        elif daily_return is None or not math.isfinite(daily_return) or daily_return <= -1:
            raise ValueError(f"missing or invalid daily return on {date}")
        else:
            value *= 1 + daily_return
        units[date] = value
    return units


def rebalance(values, cash, stock_weight, rates):
    """Self-financing two-leg move with the Oct-16 overlay convention.

    Sells first, each side at its own extra rate; buys are scaled together when the
    net sale proceeds cannot fund them. Returns (values, cash, cost, traded).
    """
    values = list(values)
    before = cash + values[0] + values[1]
    delta = [stock_weight * before - values[0], (1 - stock_weight) * before - values[1]]
    cost = traded = 0.0
    for i in range(2):
        if delta[i] < 0:
            sale = -delta[i]
            values[i] -= sale
            cash += sale * (1 - rates[i])
            cost += sale * rates[i]
            traded += sale
    need = sum(delta[i] * (1 + rates[i]) for i in range(2) if delta[i] > 0)
    scale = min(1.0, max(0.0, cash) / need) if need else 0.0
    for i in range(2):
        if delta[i] > 0:
            purchase = delta[i] * scale
            values[i] += purchase
            cash -= purchase * (1 + rates[i])
            cost += purchase * rates[i]
            traded += purchase
    return values, cash, cost, traded
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_v713_synthetic.py -q -p no:cacheprovider`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add app/services/v713_synthetic.py plugins/v713/synthetic_policy_v1.json tests/unit/test_v713_synthetic.py
git commit -m "feat(v713): synthetic ledger units, overlay rebalance and frozen policy"
```

---

### Task 7: 合成账本的状态与时点规则

规则（设计第六节第 3、4 小节）：

| 本月状态 \ 上月 | 非 AUX | AUX |
|---|---|---|
| 非 AUX | `VIRTUAL`：锚定日收盘，两侧都按虚拟计费 | `EXIT_AUX`：锚定日收盘，只对 Hydra 一侧计费 |
| AUX | `ENTER_AUX`：从锚定日起，等到股票腿股票市值 ≤ 1% 的那天收盘才换，只对 Hydra 一侧计费；等待超过 10 个交易日记 `REVIEW_REQUIRED` | 无动作 |

等待清仓时下个月已到期，则停止并返回 `REVIEW_REQUIRED`，不自动处理。`all_virtual=True` 用于研究回放：所有调整都按 `VIRTUAL` 处理。

**Files:**
- Modify: `app/services/v713_synthetic.py`
- Test: `tests/unit/test_v713_synthetic.py`

- [ ] **Step 1: Write the failing test**

追加 import：`from app.services.v713_synthetic import MonthState, run_synthetic`，并追加：

```python
D = [f"202612{x:02d}" for x in range(1, 21)]
US = {d: 1.0 + .001 * i for i, d in enumerate(D)}
UH = {d: 1.0 for d in D}
T1 = MonthState("20261130", "T1_5050", D[0])
AUX_MONTH = MonthState("20261231", "AUX_HYDRA", D[2])
TOP = MonthState("20270129", "TOP50", D[10])


def _run(months, exposure=None, dates=D, **changes):
    return run_synthetic(POLICY, **({"dates": dates, "unit_stock": US, "unit_hydra": UH,
                                     "exposure": exposure or {d: 0.0 for d in D}, "months": months,
                                     "bps": 20.0} | changes))


def test_init_splits_the_base_by_state_without_cost():
    out = _run([T1])
    assert out["status"] == "CURRENT"
    assert out["events"][0]["kind"] == "INIT" and out["events"][0]["cost"] == 0.0
    assert out["rows"][0]["value"] == pytest.approx(400_000.0)
    assert out["rows"][0]["stock_weight"] == pytest.approx(.5)


def test_virtual_switch_charges_both_sides():
    out = _run([MonthState("20261130", "TOP50", D[0]), MonthState("20261231", "T1_5050", D[5])])
    move = out["events"][1]
    assert (move["kind"], move["date"]) == ("VIRTUAL", D[5])
    sold = 400_000.0 * US[D[5]] / 2
    assert move["cost"] == pytest.approx(sold * .002 + sold * (1 - .002) / (1 + .002) * .002)


def test_aux_entry_waits_for_real_liquidation_then_exit_converts_back():
    exposure = {d: (.99 if d < "20261205" else 0.0) for d in D}
    out = _run([T1, AUX_MONTH, TOP], exposure=exposure)
    kinds = [(e["date"], e["kind"]) for e in out["events"]]
    assert kinds == [(D[0], "INIT"), ("20261205", "ENTER_AUX"), (D[10], "EXIT_AUX")]
    assert [r["date"] for r in out["rows"] if r["pending"]] == ["20261203", "20261204"]
    stock_value = 200_000.0 * US["20261205"]
    assert out["events"][1]["cost"] == pytest.approx(stock_value * .002 / 1.002)
    hydra_value = 200_000.0 + stock_value / 1.002
    assert out["events"][2]["cost"] == pytest.approx(hydra_value * .002)
    assert out["rows"][-1]["stock_weight"] == pytest.approx(1.0)


def test_unfinished_liquidation_is_flagged_and_stops_at_the_next_month():
    stuck = {d: .99 for d in D}
    flagged = _run([T1, AUX_MONTH], exposure=stuck)
    assert flagged["status"] == "REVIEW_REQUIRED"
    assert (flagged["events"][-1]["date"], flagged["events"][-1]["kind"]) == (D[12], "REVIEW_REQUIRED")
    assert len(flagged["rows"]) == len(D)
    stopped = _run([T1, AUX_MONTH, TOP], exposure=stuck)
    assert stopped["status"] == "REVIEW_REQUIRED"
    assert stopped["events"][-1]["kind"] == "STOPPED" and stopped["rows"][-1]["date"] == D[9]


def test_missing_leg_nav_on_an_anchor_is_an_error():
    with pytest.raises(ValueError, match="20261203"):
        _run([T1, AUX_MONTH], dates=[d for d in D if d != D[2]])
    assert _run([])["status"] == "WAITING_START"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_v713_synthetic.py -q -p no:cacheprovider`
Expected: collection error `ImportError: cannot import name 'run_synthetic'`

- [ ] **Step 3: Write minimal implementation**

`app/services/v713_synthetic.py` 末尾加：

```python
def run_synthetic(policy: SyntheticPolicy, *, dates, unit_stock, unit_hydra, exposure, months, bps,
                  all_virtual=False) -> dict:
    """Daily synthetic path at one overlay cost.

    ``exposure`` maps a date to the stock leg's stock market value / NAV. With
    ``all_virtual`` (research replay) every move charges both sides; otherwise a leg
    that trades for real is not charged again.
    """
    rate = bps / 10_000
    months = sorted(months, key=lambda month: month.signal_date)
    for month in months:
        if month.sleeve not in STATE_STOCK_WEIGHT:
            raise ValueError(f"unknown sleeve {month.sleeve}")
    dates = sorted(dates)
    if not months:
        return {"status": "WAITING_START", "rows": [], "events": []}
    if months[0].anchor_date not in dates:
        return {"status": "WAITING_LEG_NAV", "rows": [], "events": []}
    first, pending = months[0], months[1:]
    start = first.anchor_date
    weight = STATE_STOCK_WEIGHT[first.sleeve]
    units = [weight * policy.base_value / unit_stock[start], (1 - weight) * policy.base_value / unit_hydra[start]]
    cash, sleeve, status = 0.0, first.sleeve, "CURRENT"
    events = [{"date": start, "kind": "INIT", "signal_date": first.signal_date, "sleeve": first.sleeve,
               "to_stock_weight": weight, "traded": 0.0, "cost": 0.0,
               "value_before": policy.base_value, "value_after": policy.base_value}]
    rows, waiting = [], None

    def move(date, month, kind, rates):
        nonlocal units, cash, sleeve
        values = [units[0] * unit_stock[date], units[1] * unit_hydra[date]]
        before = cash + values[0] + values[1]
        values, cash, cost, traded = rebalance(values, cash, STATE_STOCK_WEIGHT[month.sleeve], rates)
        units = [values[0] / unit_stock[date], values[1] / unit_hydra[date]]
        events.append({"date": date, "kind": kind, "signal_date": month.signal_date, "sleeve": month.sleeve,
                       "to_stock_weight": STATE_STOCK_WEIGHT[month.sleeve], "traded": traded, "cost": cost,
                       "value_before": before, "value_after": cash + values[0] + values[1]})
        sleeve = month.sleeve

    for date in dates[dates.index(start):]:
        if date != start:
            due = pending[0] if pending and pending[0].anchor_date <= date else None
            if due is not None and due.anchor_date != date:
                raise ValueError(f"leg NAV missing on anchor date {due.anchor_date}")
            if due is not None and waiting is not None:
                status = "REVIEW_REQUIRED"
                events.append({"date": date, "kind": "STOPPED", "signal_date": due.signal_date,
                               "sleeve": due.sleeve})
                break
            if due is not None:
                pending.pop(0)
                if due.sleeve == AUX and sleeve == AUX:
                    pass
                elif due.sleeve == AUX and not all_virtual:
                    waiting = [due, 0]
                elif sleeve == AUX and not all_virtual:
                    move(date, due, "EXIT_AUX", (0.0, rate))
                else:
                    move(date, due, "VIRTUAL", (rate, rate))
            if waiting is not None:
                if date not in exposure:
                    raise ValueError(f"stock-leg valuation missing on {date}")
                if exposure[date] <= policy.liquidated_stock_weight:
                    move(date, waiting[0], "ENTER_AUX", (0.0, rate))
                    waiting = None
                else:
                    waiting[1] += 1
                    if waiting[1] == policy.review_after_sessions + 1:
                        status = "REVIEW_REQUIRED"
                        events.append({"date": date, "kind": "REVIEW_REQUIRED",
                                       "signal_date": waiting[0].signal_date, "sleeve": waiting[0].sleeve})
        values = [units[0] * unit_stock[date], units[1] * unit_hydra[date]]
        value = cash + values[0] + values[1]
        rows.append({"date": date, "sleeve": sleeve, "unit_stock": unit_stock[date],
                     "unit_hydra": unit_hydra[date], "stock_units": units[0], "hydra_units": units[1],
                     "cash": cash, "value": value, "stock_weight": values[0] / value,
                     "pending": None if waiting is None else "ENTER_AUX"})
    return {"status": status, "rows": rows, "events": events}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_v713_synthetic.py -q -p no:cacheprovider`
Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add app/services/v713_synthetic.py tests/unit/test_v713_synthetic.py
git commit -m "feat(v713): synthetic ledger state and timing rules"
```

---

### Task 8: 与 10/16 研究引擎对拍

新引擎在 `all_virtual=True`、按月锚定时必须与 `tools/v713_combination_validation.py::simulate_periods` 完全一致；这把 10/16 已验证的 65 项数学测试传递到新代码上。

**Files:**
- Test: `tests/unit/test_v713_synthetic.py`

- [ ] **Step 1: Write the test**

追加：

```python
import importlib.util
import random

RESEARCH = Path(__file__).resolve().parents[4] / "tools" / "v713_combination_validation.py"


def _research():
    spec = importlib.util.spec_from_file_location("v713_combination_research", RESEARCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("bps", [0.0, 20.0, 50.0])
def test_all_virtual_replay_equals_the_oct16_engine(bps):
    rng = random.Random(7)
    bounds = [f"{2020 + k // 12}-{k % 12 + 1:02d}-28" for k in range(61)]
    rows = [{"start": a, "end": b, "known_at": a, "sleeve": rng.choices(["TOP50", "T1_5050", "AUX_HYDRA"], (5, 4, 1))[0],
             "stock_return": rng.uniform(-.1, .1), "hydra_return": rng.uniform(-.03, .03)}
            for a, b in zip(bounds, bounds[1:])]
    reference = _research().simulate_periods(rows, initial_capital=400_000, rebalance="monthly",
                                             switch_bps=bps)["summary"]["final_nav"]
    out = run_synthetic(POLICY, dates=bounds, exposure={}, bps=bps, all_virtual=True,
                        unit_stock=unit_nav([(bounds[0], None)] + [(r["end"], r["stock_return"]) for r in rows]),
                        unit_hydra=unit_nav([(bounds[0], None)] + [(r["end"], r["hydra_return"]) for r in rows]),
                        months=[MonthState(r["start"], r["sleeve"], r["start"]) for r in rows])
    assert out["rows"][-1]["value"] == pytest.approx(reference, rel=1e-12)
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_v713_synthetic.py -q -p no:cacheprovider -k oct16`
Expected: `3 passed`。若失败，先核对 `rebalance` 与 `simulate_periods` 第 164–189 行的卖先买后、等比缩放约定，不要放宽容差。

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_v713_synthetic.py
git commit -m "test(v713): synthetic ledger replays the Oct-16 research engine exactly"
```

---

### Task 9: 读取数据库输入与每日任务

**Files:**
- Modify: `app/services/v713_synthetic.py`（末尾加 `load_inputs`）
- Create: `scripts/run_v713_synthetic.py`
- Modify: `app/settings.py`（`hydra_monthly_instance_id` 之后）
- Test: `tests/unit/test_v713_synthetic.py`

- [ ] **Step 1: Write the failing test**

追加：

```python
from app.db import init_db, make_engine, make_session_factory
from app.models import PerfSnapshot, PerfValuation
from app.oms.models import OmsCycle, V713StateRecord
from scripts import run_v713_synthetic


def _database(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/synthetic.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        for i, day in enumerate(D[:5]):
            s.add(PerfSnapshot(instance_id="live_v713_stockleg", date=day, execution_domain="live",
                               nav=200_000.0 * 1.01 ** i, daily_return=None if i == 0 else .01,
                               positions_snapshot={}))
            s.add(PerfSnapshot(instance_id="live_hydra_v481_rb", date=day, execution_domain="live",
                               nav=300_000.0, daily_return=None if i == 0 else 0.0, positions_snapshot={}))
            s.add(PerfValuation(instance_id="live_v713_stockleg", date=day, execution_domain="live",
                                nav=200_000.0, cash=2_000.0, dividend_receivable=0.0))
        s.add(V713StateRecord(instance_id="live_v713_stockleg", signal_date="20261130", sleeve="T1_5050",
                              decision_date=D[1], basket_sha256="b" * 64, allocation_sha256="a" * 64,
                              target_version_id="tv_x", recorded_at="2026-11-30T20:00:00+08:00"))
        s.add(OmsCycle(cycle_id="C00001", cycle_no=1, target_version_id="tv_x", account_alias="shared",
                       instance_id="live_v713_stockleg", policy={}, nav_at_signal=200_000.0, frozen_target={},
                       sell_anchor={}, lot_gap=0.0, status="ACTIVE", created_at="2026-11-30T20:00:00+08:00",
                       schedule=[{"seq": 1, "trade_date": D[0], "phase": "SELL", "sell_attempt": 0}]))
        s.commit()
    return sf


def test_job_reads_leg_navs_and_states_and_writes_every_cost_line(tmp_path):
    sf = _database(tmp_path)
    output = tmp_path / "v713_synthetic.json"
    assert run_v713_synthetic.main(["--policy", str(POLICY_FILE), "--output", str(output)], session_factory=sf,
                                   now="2026-12-05T17:00:00+08:00") == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["policy_id"] == "V713_SYNTHETIC_V1" and document["status"] == "CURRENT"
    assert set(document["lines"]) == {"0", "20", "50"}
    rows = document["lines"]["20"]["rows"]
    assert rows[0]["date"] == D[0]  # the cycle's first session, not the decision date
    assert rows[-1]["value"] == pytest.approx(400_000.0 * (.5 * 1.01 ** 4 + .5))
    assert not list(tmp_path.glob("*.tmp"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_v713_synthetic.py -q -p no:cacheprovider -k job`
Expected: collection error `ImportError: cannot import name 'run_v713_synthetic' from 'scripts'`

- [ ] **Step 3: Write minimal implementation**

`app/settings.py`，在 `hydra_monthly_instance_id: str = "live_hydra_v481_rb"` 之后加（环境变量 `QMT_V713_SYNTHETIC_OUTPUT`）：

```python
    v713_synthetic_output: Path | None = None
```

`app/services/v713_synthetic.py` 末尾加：

```python
def load_inputs(session_factory, policy: SyntheticPolicy) -> dict:
    """Leg unit NAVs, stock-leg exposure and monthly states from the live database."""
    from sqlalchemy import select
    from app.models import PerfSnapshot, PerfValuation
    from app.oms.models import OmsCycle, V713StateRecord

    with session_factory() as session:
        def snapshots(instance_id):
            return [(row.date, row.daily_return) for row in session.scalars(select(PerfSnapshot).where(
                PerfSnapshot.instance_id == instance_id, PerfSnapshot.execution_domain == "live"))]

        unit_stock = unit_nav(snapshots(policy.stock_instance))
        unit_hydra = unit_nav(snapshots(policy.hydra_instance))
        exposure = {row.date: max(0.0, (row.nav - row.cash - row.dividend_receivable) / row.nav)
                    for row in session.scalars(select(PerfValuation).where(
                        PerfValuation.instance_id == policy.stock_instance,
                        PerfValuation.execution_domain == "live"))}
        months = []
        for record in session.scalars(select(V713StateRecord).where(
                V713StateRecord.instance_id == policy.stock_instance).order_by(V713StateRecord.signal_date)):
            anchor = record.decision_date
            if record.target_version_id is not None:
                cycle = session.scalars(select(OmsCycle).where(
                    OmsCycle.target_version_id == record.target_version_id)).one()
                anchor = cycle.schedule[0]["trade_date"]
            months.append(MonthState(record.signal_date, record.sleeve, anchor))
    return {"dates": sorted(set(unit_stock) & set(unit_hydra)), "unit_stock": unit_stock,
            "unit_hydra": unit_hydra, "exposure": exposure, "months": months}
```

`scripts/run_v713_synthetic.py`：

```python
"""Daily read-only V713 synthetic ledger; never creates targets or orders.

    PYTHONPATH=. python -m scripts.run_v713_synthetic --policy plugins/v713/synthetic_policy_v1.json \
        --output /path/to/v713_synthetic.json

Run after the live performance job has valued both legs for the day.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def main(argv=None, *, session_factory=None, now=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.run_v713_synthetic")
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="defaults to QMT_V713_SYNTHETIC_OUTPUT")
    args = parser.parse_args(argv)

    from app.services.v713_synthetic import load_inputs, load_synthetic_policy, run_synthetic
    policy = load_synthetic_policy(args.policy)
    output = args.output
    if session_factory is None or output is None:
        from app.settings import get_settings
        settings = get_settings()
        output = output or settings.v713_synthetic_output
        if session_factory is None:
            from app.db import make_engine, make_session_factory
            session_factory = make_session_factory(make_engine(settings.db_url))
    if output is None:
        raise SystemExit("--output or QMT_V713_SYNTHETIC_OUTPUT is required")
    inputs = load_inputs(session_factory, policy)
    lines = {f"{bps:g}": run_synthetic(policy, bps=bps, **inputs) for bps in policy.cost_bps}
    primary = lines[f"{policy.primary_bps:g}"]
    document = {"policy_id": policy.policy_id, "label": policy.label, "stock_instance": policy.stock_instance,
                "hydra_instance": policy.hydra_instance, "base_value": policy.base_value,
                "primary_bps": policy.primary_bps, "status": primary["status"],
                "generated_at": now or _now(), "lines": lines}
    output = Path(output)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, output)
    print(json.dumps({"status": primary["status"], "rows": len(primary["rows"]), "output": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_v713_synthetic.py -q -p no:cacheprovider`
Expected: `13 passed`

- [ ] **Step 5: Commit**

```bash
git add app/services/v713_synthetic.py app/settings.py scripts/run_v713_synthetic.py tests/unit/test_v713_synthetic.py
git commit -m "feat(v713): daily synthetic ledger job over the live database"
```

---

### Task 10: 只读接口与看板页

**Files:**
- Modify: `app/oms/api.py`（import 与新路由）
- Modify: `app/oms/dashboard.py`
- Create: `app/web/v713/index.html`
- Test: `tests/unit/test_v713_synthetic_api.py`

- [ ] **Step 1: Write the failing test**

```python
"""Read-only synthetic V713 endpoint and dashboard page."""
import json

from fastapi.testclient import TestClient

from app.dependencies import _engine_for_url, get_settings
from app.main import create_app
from app.settings import Settings

LIVE = {"Authorization": "Bearer LIVE_KEY"}
OPERATOR = {"Authorization": "Bearer OP_KEY"}


def _client(tmp_path, output):
    get_settings.cache_clear()
    _engine_for_url.cache_clear()
    settings = Settings(paper_api_key="PAPER_KEY", live_api_key="LIVE_KEY", oms_operator_api_key="OP_KEY",
                        live_client_id="hydra-live-client", live_account_aliases_csv="hydra-live",
                        oms_live_enabled=True, db_url=f"sqlite:///{tmp_path}/api.db",
                        parquet_root=tmp_path / "data", plugins_dir=tmp_path / "plugins",
                        strategies_file=tmp_path / "strategies.yaml", log_level="WARNING",
                        v713_synthetic_output=output)
    return TestClient(create_app(settings_override=settings))


def test_operator_reads_the_synthetic_document(tmp_path):
    output = tmp_path / "v713_synthetic.json"
    output.write_text(json.dumps({"policy_id": "V713_SYNTHETIC_V1", "lines": {}}), encoding="utf-8")
    client = _client(tmp_path, output)
    body = client.get("/oms/live/v713/synthetic", params={"account_alias": "hydra-live"}, headers=OPERATOR).json()
    assert body["code"] == 0 and body["data"]["policy_id"] == "V713_SYNTHETIC_V1"
    assert client.get("/oms/live/v713/synthetic", params={"account_alias": "hydra-live"},
                      headers=LIVE).status_code == 403


def test_missing_document_is_reported_not_invented(tmp_path):
    client = _client(tmp_path, tmp_path / "absent.json")
    response = client.get("/oms/live/v713/synthetic", params={"account_alias": "hydra-live"}, headers=OPERATOR)
    assert response.status_code == 404


def test_dashboard_page_is_served(tmp_path):
    page = _client(tmp_path, None).get("/dashboard/v713-synthetic")
    assert page.status_code == 200 and "V713 合成净值" in page.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_v713_synthetic_api.py -q -p no:cacheprovider`
Expected: FAIL（`/oms/live/v713/synthetic` 返回 404 且 body 无 `code == 0`；看板页 404）

- [ ] **Step 3: Write minimal implementation**

`app/oms/api.py`：在 `from typing import Literal` 后加 `import json`、`from pathlib import Path`，在 `get_overview` 之后加：

```python
@router.get("/v713/synthetic", response_model=APIResponse[dict])
def get_v713_synthetic(account_alias: str, auth: AuthContext = Depends(verify_api_key),
                       settings: Settings = Depends(get_settings)):
    _authorize_operator(auth, account_alias)
    path = settings.v713_synthetic_output
    if path is None or not Path(path).is_file():
        raise APIError(ErrorCode.BAD_REQUEST, "V713 合成账本尚未生成", http_status=404)
    return APIResponse[dict](code=0, message="ok", data=json.loads(Path(path).read_text(encoding="utf-8")))
```

`app/oms/dashboard.py` 改为：

```python
"""Static dashboard pages. They carry no data and load everything with the operator key
the user types in (kept only in the tab's sessionStorage)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()
WEB = Path(__file__).resolve().parents[1] / "web"
PAGE = WEB / "oms" / "index.html"
V713_PAGE = WEB / "v713" / "index.html"


def _page(path: Path) -> HTMLResponse:
    # Read per request: a missing page must never prevent the trading API from starting.
    return HTMLResponse(path.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer"})


@router.get("/dashboard/oms", response_class=HTMLResponse, include_in_schema=False)
def oms_dashboard() -> HTMLResponse:
    return _page(PAGE)


@router.get("/dashboard/v713-synthetic", response_class=HTMLResponse, include_in_schema=False)
def v713_synthetic_dashboard() -> HTMLResponse:
    return _page(V713_PAGE)
```

`app/web/v713/index.html`：

```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V713 合成净值</title>
<style>
  body { font: 14px/1.5 system-ui, sans-serif; margin: 16px; color: #1f2328; background: #fff; }
  input, button { font: inherit; padding: 4px 8px; }
  table { border-collapse: collapse; margin-top: 12px; width: 100%; }
  th, td { border-bottom: 1px solid #d0d7de; padding: 4px 8px; text-align: right; }
  th:first-child, td:first-child { text-align: left; }
  .status { font-weight: 600; }
  .status.REVIEW_REQUIRED { color: #cf222e; }
  svg { width: 100%; height: 220px; margin-top: 12px; }
</style>
</head>
<body>
<h1>V713 合成净值</h1>
<p id="label"></p>
<label>运维密钥 <input id="key" type="password" autocomplete="off"></label>
<label>账户 <input id="account" autocomplete="off"></label>
<button id="load">读取</button>
<p>状态：<span id="status" class="status">未读取</span> · 生成于 <span id="generated">—</span></p>
<svg id="chart" viewBox="0 0 600 220" preserveAspectRatio="none"></svg>
<h2>最近 20 个交易日（主线）</h2>
<table id="rows"><thead><tr><th>日期</th><th>状态</th><th>合成值</th><th>股票腿占比</th><th>待完成</th></tr></thead><tbody></tbody></table>
<h2>事件</h2>
<table id="events"><thead><tr><th>日期</th><th>类型</th><th>月份</th><th>状态</th><th>虚拟成本</th></tr></thead><tbody></tbody></table>
<script>
const $ = (id) => document.getElementById(id);
for (const id of ["key", "account"]) {
  try { $(id).value = sessionStorage.getItem("v713." + id) || ""; } catch (e) {}
}
function cell(text) { const td = document.createElement("td"); td.textContent = text; return td; }
function fill(table, rows) {
  const body = $(table).querySelector("tbody");
  body.replaceChildren(...rows.map((values) => { const tr = document.createElement("tr"); tr.append(...values.map(cell)); return tr; }));
}
function chart(lines) {
  const svg = $("chart"); svg.replaceChildren();
  const all = Object.values(lines).flatMap((line) => line.rows.map((row) => row.value));
  if (!all.length) return;
  const lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1;
  const colors = {"0": "#8c959f", "20": "#0969da", "50": "#bf8700"};
  for (const [bps, line] of Object.entries(lines)) {
    const n = Math.max(line.rows.length - 1, 1);
    const points = line.rows.map((row, i) => `${(i / n) * 600},${210 - ((row.value - lo) / span) * 200}`).join(" ");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    path.setAttribute("points", points); path.setAttribute("fill", "none");
    path.setAttribute("stroke", colors[bps] || "#1f2328"); path.setAttribute("stroke-width", "2");
    svg.append(path);
  }
}
$("load").addEventListener("click", async () => {
  try { sessionStorage.setItem("v713.key", $("key").value); sessionStorage.setItem("v713.account", $("account").value); } catch (e) {}
  const url = "/oms/live/v713/synthetic?account_alias=" + encodeURIComponent($("account").value);
  const response = await fetch(url, {headers: {"Authorization": "Bearer " + $("key").value}});
  const body = await response.json().catch(() => ({}));
  if (!response.ok || body.code !== 0) { $("status").textContent = body.message || ("HTTP " + response.status); return; }
  const doc = body.data, primary = doc.lines[String(doc.primary_bps)] || {rows: [], events: []};
  $("label").textContent = doc.label + " · 主线 " + doc.primary_bps + "bp（另有 0bp、50bp）";
  $("status").textContent = doc.status; $("status").className = "status " + doc.status;
  $("generated").textContent = doc.generated_at;
  chart(doc.lines);
  fill("rows", primary.rows.slice(-20).reverse().map((r) => [r.date, r.sleeve, r.value.toFixed(2), (r.stock_weight * 100).toFixed(1) + "%", r.pending || ""]));
  fill("events", primary.events.slice().reverse().map((e) => [e.date, e.kind, e.signal_date, e.sleeve, e.cost === undefined ? "" : e.cost.toFixed(2)]));
});
</script>
</body>
</html>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_v713_synthetic_api.py tests/unit/test_oms_manual.py tests/unit/test_oms_api.py -q -p no:cacheprovider`
Expected: 全部通过

- [ ] **Step 5: Commit**

```bash
git add app/oms/api.py app/oms/dashboard.py app/web/v713/index.html tests/unit/test_v713_synthetic_api.py
git commit -m "feat(v713): read-only synthetic ledger endpoint and dashboard page"
```

---

### Task 11: 全量回归、远程回放和设计文档同步

**Files:**
- Modify: `docs/V713_STOCK_LEG_SYNTHETIC_DESIGN_20261023.md`（第六节第 5 小节、第八节 E1 与 E6 行）

- [ ] **Step 1: 同步设计文档**

第六节第 5 小节末尾加一句：“结果写入一个 JSON 文件并原子替换（`QMT_V713_SYNTHETIC_OUTPUT`），由 `GET /oms/live/v713/synthetic` 只读提供；每月状态记录在 `v713_state_records` 表。”第八节 E6 行把“结果写入 `ParquetStore`”改为“结果写入 JSON 文件（原子替换）”；E1 行末尾加“每月状态写入 `v713_state_records`”。

- [ ] **Step 2: 全量单元测试**

Run: `python -m pytest tests/unit -q -p no:cacheprovider`
Expected: 全部通过；与本计划开始前相比，新增 `test_oms_stock_leg.py` 12 项、`test_v713_synthetic.py` 13 项、`test_v713_synthetic_api.py` 3 项，没有新增失败。

- [ ] **Step 3: 在 qmt 上用 10/16 月度面板回放（新隔离目录，只读输入）**

```bash
RUN=/opt/qmt-server/private/research-runs/v713-synthetic-replay-$(date +%Y%m%d)-01
SRC=/opt/qmt-server/private/research-runs/v713-combination-validation-20261016
test ! -e "$RUN" && mkdir "$RUN"
cd /opt/qmt-server/v2.3/server  # 已部署本计划代码的 checkout；只读，不重启服务
PYTHONDONTWRITEBYTECODE=1 /opt/qmt-server/venv/bin/python - "$SRC/hydra/monthly_panel.json" "$SRC/combination/combination_summary.json" "$RUN/replay.json" <<'PY'
import json, sys
from app.services.v713_synthetic import MonthState, SyntheticPolicy, run_synthetic, unit_nav
rows = json.load(open(sys.argv[1]))["rows"]
cases = json.load(open(sys.argv[2]))["cases"]
policy = SyntheticPolicy("V713_SYNTHETIC_REPLAY", "s", "h", "replay")
bounds = [rows[0]["start"]] + [r["end"] for r in rows]
unit_stock = unit_nav([(bounds[0], None)] + [(r["end"], r["stock_return"]) for r in rows])
unit_hydra = unit_nav([(bounds[0], None)] + [(r["end"], r["hydra_return"]) for r in rows])
months = [MonthState(r["start"], r["sleeve"], r["start"]) for r in rows]
out = {}
for name, bps in (("original_monthly", 0.0), ("overlay_5bps", 5.0), ("overlay_20bps", 20.0), ("overlay_50bps", 50.0)):
    value = run_synthetic(policy, dates=bounds, unit_stock=unit_stock, unit_hydra=unit_hydra, exposure={},
                          months=months, bps=bps, all_virtual=True)["rows"][-1]["value"]
    reference = cases[name]["final_nav"]
    out[name] = {"replay": value, "oct16": reference, "relative_error": value / reference - 1}
    assert abs(value / reference - 1) <= 1e-12, out[name]
json.dump(out, open(sys.argv[3], "w"), indent=2)
print(json.dumps(out, indent=1))
PY
```

Expected: 四个情景的 `relative_error` 绝对值都 ≤ 1e-12。2026-10-23 用本计划同一份引擎代码的原型在 qmt 上跑过这一步（`research-runs/v713-synthetic-proto-replay-20261023-01`），结果：`original_monthly` 3,208,589.57，与 10/16 相差 2e-16；5bp、20bp、50bp 相对主代理分别为 −1.39%、−5.44%、−13.04%，最大误差 8e-16，与 10/16 报告一致。

- [ ] **Step 4: Commit**

```bash
git add docs/V713_STOCK_LEG_SYNTHETIC_DESIGN_20261023.md
git commit -m "docs(v713): record synthetic ledger storage and state records"
```

---

## 上线操作（不在本计划代码范围，供 E4 与部署时使用）

1. **期初（E4）**：资金负责人确认后，按 Handoff 第 77 行的流程以零资源创建 `live_v713_stockleg`（`ledger_mode=attributed`），再用 `/accounts/capital-transfers` 从账户未分配现金划入 20 万元，保存不可变回执。
2. **每月发布**：V713 篮子更新后，先 dry-run，核对逐股清单，再 `--apply`、`approve --yes`：

```bash
PYTHONPATH=. python -m scripts.oms_ops publish-v713-stock-leg --instance live_v713_stockleg --account <alias> \
  --basket plugins/v713/data/v713_target_latest.parquet --policy-template <V713_STOCKLEG_v1 模板> \
  --closes <当日不复权收盘价.json> --calendar <交易日历.json>
```

3. **每日收盘后**：先运行 `scripts.run_live_performance --instance live_v713_stockleg --instance live_hydra_v481_rb --raw-price-evidence <证据>`，再运行 `scripts.run_v713_synthetic --policy plugins/v713/synthetic_policy_v1.json`（设置 `QMT_V713_SYNTHETIC_OUTPUT`）。定时器和部署需单独批准。
4. **首轮**：最早用 as-of 2026-11-30 的篮子，执行日 2026-12-01。
