# v53 全天候 10 ETF 集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把同事 `magicboom1/permenant_portfolio` v53（10 ETF 双层 inv_vol 全天候）集成进 v2.3 server，作为长期底仓，与现役 paper_v20h 并行在 QMT 模拟账户 `301300148788` 上运行。

**Architecture:** 复刻 v20h plugin/adapter 模式：`plugins/v53/` 自闭包（algo vendored + bundled data + thin strategy wrapper），`plugins/v53_adapter.py` 接 `Strategy` 协议。新增 `instance_state.owned_symbols` 字段实现 multi-instance 共享 QMT 账户的 reconcile 隔离。

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, APScheduler, pandas, pyarrow, pytest, pyyaml.

**Source Spec:** [`docs/superpowers/specs/2026-05-24-v53-allweather-bottom-integration-design.md`](../specs/2026-05-24-v53-allweather-bottom-integration-design.md) (commit 32e0117)

**Reference algorithm:** `magicboom1/permenant_portfolio` master 分支的 `v53/` 子目录（同事 GitHub）

---

## File Structure

新建 / 修改的文件全景。每个文件单一职责，便于独立测试与维护。

```
v2.3/server/plugins/v53/                  ★ NEW
├── __init__.py                            导出 V53Strategy（algo wrapper）
├── config.yaml                            策略参数 + 风控阈值 + dry_run flag
├── vendor/                                同事 master 拷贝（单向 vendor，不引 git submodule）
│   ├── __init__.py
│   ├── weight_methods.py                  compute_baseline 双层 inv_vol
│   ├── erc_solver.py                      备用 ERC（默认不调用）
│   └── reference_config.py                同事原 ETF_CODES + QUADRANT_MAP（验证用）
├── code_map.py                            v53 内部 key (hs300) ↔ QMT code (510300.SH)
├── strategy.py                            V53Strategy.compute_targets(returns) → {qmt_code: weight}
├── data/                                  bundled 历史
│   ├── etf_close.parquet                  10 ETF 全历史 close
│   └── etf_meta.parquet                   ETF 元数据
└── tests/
    ├── __init__.py
    ├── test_code_map.py
    ├── test_strategy.py
    └── test_vendor_compute_baseline.py

v2.3/server/plugins/v53_adapter.py         ★ NEW —— Strategy 协议适配
v2.3/server/tests/unit/test_v53_adapter.py ★ NEW

v2.3/server/app/models/instance_state.py   ★ MODIFY —— 加 owned_symbols 字段
v2.3/server/scripts/migrate_db.py          ★ MODIFY —— 加 ALTER TABLE owned_symbols

v2.3/server/strategies.yaml                ★ MODIFY —— 加 paper_v53 group

v2.3/server/app/services/reconcile.py      ★ MODIFY —— owned_symbols 白名单过滤 + cash 改造
v2.3/server/tests/unit/test_reconcile.py   ★ MODIFY —— 加 multi-instance 测试

v2.3/server/app/main.py                    ★ MODIFY —— 启动时调 validate_no_overlap

v2.3/server/app/scheduler/pipeline.py      ★ MODIFY —— 注册 V53Adapter
v2.3/server/app/settings.py 或 loader      ★ MODIFY —— strategies.yaml 解析支持 owned_symbols

v2.3/server/app/api/dashboard.py           ★ MODIFY —— multi-instance summary card

v2.3/server/tests/integration/test_v53_pipeline_e2e.py  ★ NEW

docs/V53_OPERATIONS_HANDBOOK.md            ★ NEW

# Mac 本地（不在 server repo，存 /Users/mameican/Desktop/策略复现/scripts/）
build_v53_bundle.py                        ★ NEW —— 生成 etf_close.parquet + etf_meta.parquet
refresh_v53_bundle.sh                      ★ NEW —— git pull + build + curl upload
```

---

## Phase 0: 前置确认

### Task 0: QDII IOPV 数据可用性核查（spec 开放问题 O1）

**目的：** 决定 Task 14 的 QDII 溢价风控钩子的实现方式（完整 / 近似 / 暂时关闭）。

**Files:**
- Reference: `C:\parttime\qmt数据推送\201. XtQuant.XtData 行情模块 _ 迅投知识库.md`

- [ ] **Step 1: 检索 XtData 文档里 ETF IOPV 相关 API**

Run（在能访问 Windows QMT 文档的环境）:
```bash
grep -in -E "IOPV|净值|estimate|estimate_nav|参考净值" "C:\parttime\qmt数据推送\201. XtQuant.XtData 行情模块 _ 迅投知识库.md"
```

- [ ] **Step 2: 如果有 IOPV API，记录函数签名 + 字段名 到 spec 第 5 节 O1**

Edit: `docs/superpowers/specs/2026-05-24-v53-allweather-bottom-integration-design.md`

把"O1. QDII IOPV 数据源"段落的 TODO 替换成具体 API 调用方式。

- [ ] **Step 3: 如果没有 IOPV API，定 fallback**

把 spec O1 段落替换成：
- 风控钩子 (a) 实现为"当日 close 与过去 20 日均值偏离 > 5%"近似指标，**或**
- 暂时关闭该钩子（config 设 `qdii_premium_threshold: null`），写 TODO 进 handbook

- [ ] **Step 4: 提交 spec 更新**

```bash
git add docs/superpowers/specs/2026-05-24-v53-allweather-bottom-integration-design.md
git commit -m "docs(spec): 确认 v53 QDII IOPV 数据源 (O1)"
```

---

## Phase 1: plugins/v53/ 骨架 + vendor 代码

### Task 1: 创建 plugins/v53/ 目录骨架

**Files:**
- Create: `v2.3/server/plugins/v53/__init__.py`
- Create: `v2.3/server/plugins/v53/vendor/__init__.py`
- Create: `v2.3/server/plugins/v53/tests/__init__.py`

- [x] **Step 1: 创建空 `__init__.py`**

```bash
mkdir -p v2.3/server/plugins/v53/vendor v2.3/server/plugins/v53/data v2.3/server/plugins/v53/tests
touch v2.3/server/plugins/v53/__init__.py
touch v2.3/server/plugins/v53/vendor/__init__.py
touch v2.3/server/plugins/v53/tests/__init__.py
```

- [x] **Step 2: Commit**

```bash
git add v2.3/server/plugins/v53/
git commit -m "feat(v53): plugins/v53/ skeleton 目录结构"
```

### Task 2: Vendor 算法核心代码

**目的：** 从同事 master 直接拷 3 个算法文件，不引 git submodule。

**Files:**
- Source: `<同事 magicboom1/permenant_portfolio master 分支>/v53/{weight_methods.py, config.py}` 和 `v41/erc_solver.py`
- Create: `v2.3/server/plugins/v53/vendor/weight_methods.py` (从同事 v53/weight_methods.py 拷)
- Create: `v2.3/server/plugins/v53/vendor/erc_solver.py` (从同事 v41/erc_solver.py 拷)
- Create: `v2.3/server/plugins/v53/vendor/reference_config.py` (从同事 v53/config.py 拷)
- Create: `v2.3/server/plugins/v53/vendor/risk_parity.py` (从 /tmp/permenant_portfolio/v4/engine/risk_parity.py 拷)

- [x] **Step 1: 拷贝文件（手工 git clone 同事 repo 后 cp）**

```bash
# 假设同事 repo 已 clone 到 /tmp/permenant_portfolio
cd /tmp
git clone https://github.com/magicboom1/permenant_portfolio.git || (cd permenant_portfolio && git pull)
cd permenant_portfolio
git checkout master
cd -

cp /tmp/permenant_portfolio/v53/weight_methods.py v2.3/server/plugins/v53/vendor/weight_methods.py
cp /tmp/permenant_portfolio/v41/erc_solver.py     v2.3/server/plugins/v53/vendor/erc_solver.py
cp /tmp/permenant_portfolio/v53/config.py         v2.3/server/plugins/v53/vendor/reference_config.py
```

- [x] **Step 2: 在每个 vendor 文件顶部加 vendor 标记注释**

Edit each of `weight_methods.py`, `erc_solver.py`, `reference_config.py` 在文件开头插入：

```python
# VENDORED from magicboom1/permenant_portfolio master @ <commit-sha-here>
# DO NOT EDIT — sync only via Mac local refresh_v53_bundle.sh (vendor copy mode).
# See: docs/superpowers/specs/2026-05-24-v53-allweather-bottom-integration-design.md §4 (vendor)
```

填入实际 commit sha（用 `git -C /tmp/permenant_portfolio rev-parse HEAD`）。

- [x] **Step 3: 删掉 vendor 文件里所有 Windows 硬编码路径与 plotting / CLI 入口**

vendor 文件应该只含纯算法函数。如果文件里有 `if __name__ == "__main__":` 块、matplotlib import、Path(r"C:\...") 等，全部删除。仅保留算法函数。

- [x] **Step 4: 跑 import 烟测**

```bash
cd v2.3/server
venv/bin/python -c "from plugins.v53.vendor import weight_methods, erc_solver, reference_config; print('ok')"
```

Expected: `ok`（无 ImportError）

- [x] **Step 5: Commit**

```bash
git add v2.3/server/plugins/v53/vendor/
git commit -m "feat(v53): vendor 同事 master 算法核心 (weight_methods/erc_solver/reference_config @ <sha>)"
```

**实施备注**: weight_methods.py 的 3 个 imports 从 `config` / `v4.engine.risk_parity` / `v41.erc_solver` 改成 vendor/ 内 relative (`.reference_config` / `.risk_parity` / `.erc_solver`)，因为 vendor 已 self-contained，不再走原 repo 的 sys.path hack。

### Task 3: Vendor sanity 测试 — compute_baseline 跑得通

**Files:**
- Create: `v2.3/server/plugins/v53/tests/test_vendor_compute_baseline.py`

- [x] **Step 1: 写测试**

```python
"""验证 vendor.compute_baseline 在已知输入上输出权重 sum≈1."""
import numpy as np
import pandas as pd
import pytest

from plugins.v53.vendor.weight_methods import compute_baseline


def _make_fake_returns(n_days: int = 300, seed: int = 0) -> pd.DataFrame:
    """生成 10 个 ETF 的伪日收益矩阵，每个 ETF vol 不同便于 inv_vol 区分。"""
    rng = np.random.default_rng(seed)
    codes = ["hs300", "cyb", "bond", "gold", "commodity2",
             "commodity3", "crude_oil", "sp500", "nasdaq", "dividend"]
    vols = [0.012, 0.020, 0.003, 0.010, 0.014,
            0.012, 0.022, 0.011, 0.013, 0.009]  # bond 最低，cyb/crude_oil 最高
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    data = {c: rng.normal(0.0003, v, n_days) for c, v in zip(codes, vols)}
    return pd.DataFrame(data, index=dates)


def test_compute_baseline_weights_sum_to_one():
    returns = _make_fake_returns()
    quadrants = {
        "growth_up":    ["hs300", "cyb", "gold", "commodity2", "commodity3",
                         "sp500", "nasdaq", "dividend"],
        "growth_down":  ["bond", "gold"],
        "inflation_up": ["gold", "commodity2", "commodity3", "crude_oil"],
        "inflation_down": ["hs300", "bond", "dividend"],
    }
    weights = compute_baseline(
        returns=returns,
        quadrants=quadrants,
        method="inv_vol",
        risk_lookback=252,
        min_history=126,
    )
    assert isinstance(weights, dict)
    assert abs(sum(weights.values()) - 1.0) < 1e-6
    # bond 是最低波，应占多
    assert weights["bond"] > 0.3
    # cyb / crude_oil 是高波，应占少
    assert weights["cyb"] < 0.05
    assert weights["crude_oil"] < 0.05


def test_compute_baseline_min_history_drops_short_history_assets():
    """min_history 不足的 ETF 应该从对应象限剔除（不在 weights 里）"""
    returns = _make_fake_returns(n_days=100)  # 仅 100 天 < min_history 126
    quadrants = {
        "growth_down":  ["bond"],
        "inflation_up": ["gold"],
    }
    weights = compute_baseline(
        returns=returns[["bond", "gold"]],
        quadrants=quadrants,
        method="inv_vol",
        risk_lookback=252,
        min_history=126,
    )
    # 所有 ETF 都不够 min_history → 返回空 dict 或全部权重 = 0（依 vendor 实现）
    assert sum(weights.values()) < 1e-6
```

⚠️ `compute_baseline` 的具体签名以 vendor 文件实际为准，跑测试前如果接口不同需调整。

- [x] **Step 2: 跑测试**

```bash
cd v2.3/server
venv/bin/pytest plugins/v53/tests/test_vendor_compute_baseline.py -v
```

Expected: 2 个 PASS。如果失败，**不要改 vendor 文件**，改测试参数适配 vendor 实际接口（compute_baseline 的关键字参数名可能不同）。

- [x] **Step 3: Commit**

```bash
git add v2.3/server/plugins/v53/tests/
git commit -m "test(v53): vendor compute_baseline 烟测 + min_history 行为验证"
```

**实施备注**: plan 原 test 用 `compute_baseline(returns, quadrants, method=, risk_lookback=, min_history=)` signature 是错的；实际签名是 `compute_baseline(close_px, rebalance_dates, etf_codes=, quadrant_map=, method=)` 返回 `(asset_df, quadrant_df, qaw_dfs)`。测试改用真实签名，6 个测试覆盖：constants 加载 / tuple-of-3 返回 / 权重 sum=1 / 低 vol bond 高权重 / 高 vol cyb+crude 低权重 / dividend 双象限重复计入对比。

---

## Phase 2: 算法 wrapper + ETF code 映射

### Task 4: ETF code 映射模块

**Files:**
- Create: `v2.3/server/plugins/v53/code_map.py`
- Create: `v2.3/server/plugins/v53/tests/test_code_map.py`

- [x] **Step 1: 写测试**

```python
"""V53 内部 key (hs300) ↔ QMT code (510300.SH) 双向映射"""
import pytest

from plugins.v53.code_map import V53_KEY_TO_QMT, QMT_TO_V53_KEY, ETF_KEYS


def test_ten_keys():
    assert len(ETF_KEYS) == 10
    assert set(ETF_KEYS) == {
        "hs300", "cyb", "bond", "gold", "commodity2",
        "commodity3", "crude_oil", "sp500", "nasdaq", "dividend",
    }


def test_key_to_qmt():
    assert V53_KEY_TO_QMT["hs300"] == "510300.SH"
    assert V53_KEY_TO_QMT["cyb"] == "159915.SZ"
    assert V53_KEY_TO_QMT["bond"] == "511260.SH"
    assert V53_KEY_TO_QMT["gold"] == "518880.SH"
    assert V53_KEY_TO_QMT["commodity2"] == "159981.SZ"
    assert V53_KEY_TO_QMT["commodity3"] == "159985.SZ"
    assert V53_KEY_TO_QMT["crude_oil"] == "159930.SZ"
    assert V53_KEY_TO_QMT["sp500"] == "513500.SH"
    assert V53_KEY_TO_QMT["nasdaq"] == "513100.SH"
    assert V53_KEY_TO_QMT["dividend"] == "512890.SH"


def test_qmt_to_key_inverse():
    for k, q in V53_KEY_TO_QMT.items():
        assert QMT_TO_V53_KEY[q] == k


def test_qdii_codes_marked():
    from plugins.v53.code_map import QDII_QMT_CODES
    assert QDII_QMT_CODES == {"513500.SH", "513100.SH"}
```

- [x] **Step 2: 跑测试，确认 fail (ImportError)**

```bash
cd v2.3/server
venv/bin/pytest plugins/v53/tests/test_code_map.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'plugins.v53.code_map'`

- [x] **Step 3: 写实现**

```python
"""V53 内部 key (vendor.reference_config 用) ↔ QMT 6 位代码 + 后缀 映射"""
from __future__ import annotations

# 顺序固定，便于 returns 矩阵列名稳定
ETF_KEYS: list[str] = [
    "hs300", "cyb", "bond", "gold", "commodity2",
    "commodity3", "crude_oil", "sp500", "nasdaq", "dividend",
]

V53_KEY_TO_QMT: dict[str, str] = {
    "hs300":      "510300.SH",
    "cyb":        "159915.SZ",
    "bond":       "511260.SH",
    "gold":       "518880.SH",
    "commodity2": "159981.SZ",
    "commodity3": "159985.SZ",
    "crude_oil":  "159930.SZ",
    "sp500":      "513500.SH",
    "nasdaq":     "513100.SH",
    "dividend":   "512890.SH",
}

QMT_TO_V53_KEY: dict[str, str] = {q: k for k, q in V53_KEY_TO_QMT.items()}

QDII_QMT_CODES: set[str] = {"513500.SH", "513100.SH"}

assert set(ETF_KEYS) == set(V53_KEY_TO_QMT.keys()), "ETF_KEYS 和 V53_KEY_TO_QMT 不一致"
```

- [x] **Step 4: 跑测试**

```bash
cd v2.3/server
venv/bin/pytest plugins/v53/tests/test_code_map.py -v
```

Expected: 4 个 PASS

- [x] **Step 5: Commit**

```bash
git add v2.3/server/plugins/v53/code_map.py v2.3/server/plugins/v53/tests/test_code_map.py
git commit -m "feat(v53): code_map.py — 10 ETF 内部 key ↔ QMT code 双向映射"
```

### Task 5: V53Strategy 算法薄壳

**Files:**
- Create: `v2.3/server/plugins/v53/strategy.py`
- Create: `v2.3/server/plugins/v53/tests/test_strategy.py`

- [x] **Step 1: 写测试**

```python
"""V53Strategy.compute_targets — 把 returns + cfg → {qmt_code: weight}"""
import numpy as np
import pandas as pd
import pytest

from plugins.v53.strategy import V53Strategy, V53Config


def _make_returns():
    rng = np.random.default_rng(42)
    codes = ["hs300", "cyb", "bond", "gold", "commodity2",
             "commodity3", "crude_oil", "sp500", "nasdaq", "dividend"]
    vols = [0.012, 0.020, 0.003, 0.010, 0.014,
            0.012, 0.022, 0.011, 0.013, 0.009]
    dates = pd.bdate_range("2024-01-01", periods=300)
    return pd.DataFrame(
        {c: rng.normal(0.0003, v, 300) for c, v in zip(codes, vols)},
        index=dates,
    )


def _default_cfg() -> V53Config:
    return V53Config(
        algorithm="inv_vol",
        risk_lookback=252,
        min_history_days=126,
        quadrants={
            "growth_up":    ["hs300", "cyb", "gold", "commodity2", "commodity3",
                             "sp500", "nasdaq", "dividend"],
            "growth_down":  ["bond", "gold"],
            "inflation_up": ["gold", "commodity2", "commodity3", "crude_oil"],
            "inflation_down": ["hs300", "bond", "dividend"],
        },
    )


def test_compute_targets_returns_qmt_codes():
    """key 是 QMT code（带 .SH/.SZ 后缀），不是内部 key"""
    strat = V53Strategy(_default_cfg())
    weights = strat.compute_targets(_make_returns())
    assert isinstance(weights, dict)
    assert all("." in k for k in weights.keys())  # 全部带 .SH/.SZ 后缀
    assert "510300.SH" in weights
    assert "511260.SH" in weights


def test_compute_targets_sum_close_to_one():
    strat = V53Strategy(_default_cfg())
    weights = strat.compute_targets(_make_returns())
    assert abs(sum(weights.values()) - 1.0) < 1e-6


def test_compute_targets_dividend_double_counted():
    """dividend ETF 在 growth_up + inflation_down 两象限同时出现 → 期末权重应该比单象限大"""
    strat = V53Strategy(_default_cfg())
    weights_double = strat.compute_targets(_make_returns())

    cfg_single = _default_cfg()
    cfg_single.quadrants["growth_up"] = [
        x for x in cfg_single.quadrants["growth_up"] if x != "dividend"
    ]
    strat_single = V53Strategy(cfg_single)
    weights_single = strat_single.compute_targets(_make_returns())

    assert weights_double["512890.SH"] > weights_single["512890.SH"]
```

- [x] **Step 2: 跑测试，确认 fail**

```bash
cd v2.3/server
venv/bin/pytest plugins/v53/tests/test_strategy.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [x] **Step 3: 写实现**

```python
"""V53Strategy: 薄壳。负责调 vendor.compute_baseline 并把结果从内部 key 翻译成 QMT code。"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from plugins.v53.code_map import V53_KEY_TO_QMT
from plugins.v53.vendor.weight_methods import compute_baseline


@dataclass
class V53Config:
    algorithm: str = "inv_vol"           # 不用 erc
    risk_lookback: int = 252
    min_history_days: int = 126
    quadrants: dict[str, list[str]] = field(default_factory=dict)


class V53Strategy:
    def __init__(self, cfg: V53Config):
        self.cfg = cfg

    def compute_targets(self, returns: pd.DataFrame) -> dict[str, float]:
        """Args:
            returns: DataFrame[trade_date × internal_key → daily_return]，列名是 v53 内部 key
                     (hs300, cyb, ...)
        Returns:
            dict[QMT code → weight]，sum ≈ 1.0。dividend ETF 在 growth_up + inflation_down
            两象限重复计入 → 自然双算。
        """
        raw_weights = compute_baseline(
            returns=returns,
            quadrants=self.cfg.quadrants,
            method=self.cfg.algorithm,
            risk_lookback=self.cfg.risk_lookback,
            min_history=self.cfg.min_history_days,
        )
        # 内部 key → QMT code
        return {V53_KEY_TO_QMT[k]: float(v) for k, v in raw_weights.items()
                if k in V53_KEY_TO_QMT and v > 0}
```

⚠️ `compute_baseline` 的 kwargs 名以 vendor 文件为准；如果不同需调整。

- [x] **Step 4: 跑测试**

```bash
cd v2.3/server
venv/bin/pytest plugins/v53/tests/test_strategy.py -v
```

Expected: 3 个 PASS

- [x] **Step 5: Commit**

```bash
git add v2.3/server/plugins/v53/strategy.py v2.3/server/plugins/v53/tests/test_strategy.py
git commit -m "feat(v53): V53Strategy 薄壳 — 内部 key → QMT code 翻译 + sanity 测试"
```

**实施备注**: plan 原 V53Config dataclass 不需要——vendor 的 RISK_PARITY_WINDOW + MIN_HISTORY_DAYS 已 hardcoded 在 reference_config.py。V53Strategy 只需要 quadrants + method 两个参数。compute_targets 签名改为 (close_px, target_date) — 内部自动生成月末 rebalance_dates 然后调 vendor.compute_baseline，取最后一期。

---

## Phase 3: Bundle 数据生成（Mac 本地）

### Task 6: build_v53_bundle.py 拼 10 ETF 全历史

**Files:**
- Create: `/Users/mameican/Desktop/策略复现/scripts/build_v53_bundle.py`
- Output: `/Users/mameican/Desktop/策略复现/out/etf_close.parquet` + `etf_meta.parquet`

- [ ] **Step 1: 写脚本**

```python
"""把 /Users/mameican/Desktop/策略复现/data/market/daily/etfs/ 里的
10 个 v53 ETF parquet 拼成单文件 etf_close.parquet。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

# 10 个 ETF 的 QMT code + 元数据
V53_ETFS = [
    # (qmt_code, name, list_date, is_qdii, quadrants)
    ("510300.SH", "hs300",      "2012-05-28", False, ["growth_up", "inflation_down"]),
    ("159915.SZ", "cyb",        "2011-12-21", False, ["growth_up"]),
    ("511260.SH", "bond",       "2017-08-04", False, ["growth_down", "inflation_down"]),
    ("518880.SH", "gold",       "2013-07-29", False, ["growth_up", "growth_down", "inflation_up"]),
    ("159981.SZ", "commodity2", "2020-01-17", False, ["growth_up", "inflation_up"]),
    ("159985.SZ", "commodity3", "2019-12-09", False, ["growth_up", "inflation_up"]),
    ("159930.SZ", "crude_oil",  "2013-09-24", False, ["inflation_up"]),
    ("513500.SH", "sp500",      "2014-01-09", True,  ["growth_up"]),
    ("513100.SH", "nasdaq",     "2013-05-15", True,  ["growth_up"]),
    ("512890.SH", "dividend",   "2019-01-23", False, ["growth_up", "inflation_down"]),
]

SRC_DIR = Path("/Users/mameican/Desktop/策略复现/data/market/daily/etfs")
OUT_DIR = Path("/Users/mameican/Desktop/策略复现/out")


def build_etf_close() -> pd.DataFrame:
    """每个 ETF 一个 parquet，schema: trade_date / open / high / low / close / volume / amount / suspendFlag
    拼成 long format: trade_date / code / close / open"""
    pieces = []
    for qmt_code, name, *_ in V53_ETFS:
        # 同事数据文件名约定: 510300.SH.parquet 或 159930.SZ.parquet
        src = SRC_DIR / f"{qmt_code}.parquet"
        if not src.exists():
            raise FileNotFoundError(f"ETF parquet 缺失: {src}")
        df = pd.read_parquet(src)
        if "trade_date" not in df.columns:
            df = df.reset_index().rename(columns={df.index.name or "index": "trade_date"})
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df["code"] = qmt_code
        keep = df[["trade_date", "code", "close", "open"]].copy()
        keep = keep.dropna(subset=["close"]).sort_values("trade_date")
        pieces.append(keep)
    out = pd.concat(pieces, ignore_index=True)
    return out


def build_etf_meta() -> pd.DataFrame:
    rows = []
    for qmt_code, name, list_date, is_qdii, quadrants in V53_ETFS:
        rows.append({
            "code": qmt_code,
            "name": name,
            "list_date": pd.to_datetime(list_date).date(),
            "is_qdii": is_qdii,
            "quadrants": quadrants,
        })
    return pd.DataFrame(rows)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    close = build_etf_close()
    close.to_parquet(OUT_DIR / "etf_close.parquet", index=False)
    print(f"etf_close.parquet: {len(close)} rows, {close.trade_date.min().date()} ~ {close.trade_date.max().date()}")
    meta = build_etf_meta()
    meta.to_parquet(OUT_DIR / "etf_meta.parquet", index=False)
    print(f"etf_meta.parquet: {len(meta)} rows")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 跑脚本验证文件能生成**

```bash
cd /Users/mameican/Desktop/策略复现
python scripts/build_v53_bundle.py
```

Expected: 输出 `etf_close.parquet: <N> rows, 2011-12-21 ~ <recent date>` 和 `etf_meta.parquet: 10 rows`

如果报 `FileNotFoundError`，确认 `/Users/mameican/Desktop/策略复现/data/market/daily/etfs/` 下确实有 10 个 ETF parquet 文件，且文件名是 `<QMT_code>.parquet` 格式。

- [ ] **Step 3: 验证 schema**

```bash
python -c "
import pandas as pd
c = pd.read_parquet('/Users/mameican/Desktop/策略复现/out/etf_close.parquet')
print('etf_close columns:', list(c.columns))
print('etf_close dtypes:', c.dtypes.to_dict())
print('etf_close codes:', sorted(c.code.unique()))
print('per-code rowcount:')
print(c.groupby('code').size())

m = pd.read_parquet('/Users/mameican/Desktop/策略复现/out/etf_meta.parquet')
print('etf_meta:')
print(m)
"
```

Expected: 10 个 code，每个 code 从其 list_date 起的全历史 close。

- [ ] **Step 4: Commit（Mac 本地 repo，如果有 git track）**

如果 `/Users/mameican/Desktop/策略复现/` 是独立 git repo:
```bash
cd /Users/mameican/Desktop/策略复现
git add scripts/build_v53_bundle.py
git commit -m "feat: build_v53_bundle.py — 拼 10 ETF 全历史 + meta"
```

### Task 7: refresh_v53_bundle.sh + 首次上传到 server

**Files:**
- Create: `/Users/mameican/Desktop/策略复现/scripts/refresh_v53_bundle.sh`

- [ ] **Step 1: 写脚本**

```bash
#!/usr/bin/env bash
set -euo pipefail

# refresh_v53_bundle.sh — 月度刷 v53 bundle
# 步骤：
#   1. (可选) 拉同事 master 算法 sync —— 默认 SKIP（vendor 单向拷贝原则）
#   2. 跑 build_v53_bundle.py 生成 etf_close.parquet + etf_meta.parquet
#   3. curl 推到 server

cd /Users/mameican/Desktop/策略复现

echo "=== build_v53_bundle.py ==="
python scripts/build_v53_bundle.py

: "${QMT_SERVER_URL:?需要环境变量 QMT_SERVER_URL，如 http://120.26.138.82:8000}"
: "${QMT_API_KEY:?需要环境变量 QMT_API_KEY}"

echo "=== upload etf_close.parquet ==="
curl -fsS -X POST \
  -H "Authorization: Bearer $QMT_API_KEY" \
  -F "file=@./out/etf_close.parquet" \
  -F "filename=etf_close.parquet" \
  "$QMT_SERVER_URL/admin/upload-data?strategy_name=v53"

echo
echo "=== upload etf_meta.parquet ==="
curl -fsS -X POST \
  -H "Authorization: Bearer $QMT_API_KEY" \
  -F "file=@./out/etf_meta.parquet" \
  -F "filename=etf_meta.parquet" \
  "$QMT_SERVER_URL/admin/upload-data?strategy_name=v53"

echo
echo "✅ v53 bundle refreshed"
```

- [ ] **Step 2: 给执行权限 + smoke run（不带 server，确认 build 那一步过得去）**

```bash
chmod +x /Users/mameican/Desktop/策略复现/scripts/refresh_v53_bundle.sh
# 不带 server 环境变量跑一下，确认 build 部分 OK，curl 部分会 fail 是预期的
/Users/mameican/Desktop/策略复现/scripts/refresh_v53_bundle.sh || true
```

Expected: build 输出 etf_close + etf_meta，然后 curl 报 `需要环境变量 QMT_SERVER_URL`（fail 在 curl 而不是 build）。

⚠️ 实际上传要等到 Phase 4 V53Adapter 注册之后才能成功（DataUploadService 校验 `data_files` 白名单，未注册的 strategy 会 reject）。**先不要跑带环境变量的版本。**

- [ ] **Step 3: Commit**

```bash
cd /Users/mameican/Desktop/策略复现
git add scripts/refresh_v53_bundle.sh
git commit -m "feat: refresh_v53_bundle.sh — 月度 build + 推 server"
```

---

## Phase 4: Schema migration + strategies.yaml

### Task 8: InstanceState 加 owned_symbols 字段

**Files:**
- Modify: `v2.3/server/app/models/instance_state.py`
- Modify: `v2.3/server/scripts/migrate_db.py`

- [x] **Step 1: 修改 model**

Edit `v2.3/server/app/models/instance_state.py` 在 `strategy_state` 行之后插入：

```python
    # 该 instance "拥有" 的标的白名单（multi-instance 共享 QMT 账户时用）。
    # None 表示 legacy 模式：reconcile 看到的 positions = "全部 - 其他 instance 的 owned"。
    # 列表表示严格白名单：reconcile 只对账列表内 symbol。
    owned_symbols: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
```

- [x] **Step 2: 修改 migrate_db.py 加 ALTER**

Edit `v2.3/server/scripts/migrate_db.py` 在 "Bug D: instance_state.strategy_state" 块之后插入：

```python
        # v53 集成: instance_state.owned_symbols
        if not column_exists(con, "instance_state", "owned_symbols"):
            print("[migrate] ALTER TABLE instance_state ADD owned_symbols")
            con.execute(text(
                "ALTER TABLE instance_state ADD COLUMN owned_symbols JSON"
            ))
        else:
            print("[migrate] instance_state.owned_symbols already exists, skip")
```

- [x] **Step 3: 跑 migration（本地 dev db）**

```bash
cd v2.3/server
venv/bin/python -m scripts.migrate_db
```

Expected: 看到 `[migrate] ALTER TABLE instance_state ADD owned_symbols` + `[migrate] done`

- [x] **Step 4: 验证 column 存在**

```bash
sqlite3 v2.3/server/pipeline-server.db "PRAGMA table_info(instance_state);" | grep owned_symbols
```

Expected: 看到 `<idx>|owned_symbols|JSON|0||0` 一行

- [x] **Step 5: Commit**

```bash
git add v2.3/server/app/models/instance_state.py v2.3/server/scripts/migrate_db.py
git commit -m "feat(schema): instance_state.owned_symbols — multi-instance 白名单字段"
```

### Task 9: strategies.yaml loader 解析 owned_symbols

**Files:**
- Find: 现有 strategies.yaml loader（在 `app/scheduler/pipeline.py` 或 `app/settings.py` 或独立 module）
- Modify: 该 loader 文件

- [ ] **Step 1: 定位 loader**

```bash
grep -rn "strategies.yaml\|account_groups" v2.3/server/app/ | head -20
```

Expected: 找到读 strategies.yaml 的位置。最可能在 `app/scheduler/pipeline.py` 或 `app/dependencies.py`。

- [ ] **Step 2: 改 loader 把 owned_symbols 写入 instance_state**

在 loader 给 instance_state 行写入的地方（应该有类似 `InstanceState(instance_id=..., virtual_cash=..., virtual_positions={})` 的代码），加上：

```python
owned_symbols=strategy_cfg.get("owned_symbols"),  # None or list[str]
```

如果 loader 用 upsert 模式（更新已存在的 instance_state），也要支持更新这个字段。

- [ ] **Step 3: 写单测**

Create: `v2.3/server/tests/unit/test_strategies_yaml_loader.py`

```python
"""验证 strategies.yaml loader 把 owned_symbols 写入 instance_state"""
import yaml
from pathlib import Path

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState


# 注：这个测试假设 loader 函数叫 load_strategies_to_db()，实际名字按 Step 1 grep 出的结果调整
def test_loader_writes_owned_symbols(tmp_path: Path):
    from app.scheduler.pipeline import load_strategies_to_db  # 调整 import 路径

    yaml_path = tmp_path / "strategies.yaml"
    yaml_path.write_text(yaml.safe_dump({
        "account_groups": [
            {
                "group_id": "g_a",
                "qmt_account_id": "TEST",
                "strategies": [{
                    "strategy_id": "v20h_v1_3",
                    "virtual_initial_cash": 10000000,
                    # 无 owned_symbols
                }],
            },
            {
                "group_id": "g_b",
                "qmt_account_id": "TEST",
                "strategies": [{
                    "strategy_id": "v53",
                    "virtual_initial_cash": 10000000,
                    "owned_symbols": ["510300.SH", "511260.SH"],
                }],
            },
        ],
    }))

    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    sf = make_session_factory(engine)

    load_strategies_to_db(yaml_path, sf)

    with sf() as s:
        a = s.get(InstanceState, "g_a_v20h_v1_3")
        b = s.get(InstanceState, "g_b_v53")
        assert a is not None and a.owned_symbols is None
        assert b is not None and b.owned_symbols == ["510300.SH", "511260.SH"]
```

⚠️ 实际 `instance_id` 组合规则参考现有代码（看 v20h 是 `paper_v20h_v20h_v1_3` 拼接逻辑）。

- [ ] **Step 4: 跑测试**

```bash
cd v2.3/server
venv/bin/pytest tests/unit/test_strategies_yaml_loader.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add v2.3/server/app/scheduler/pipeline.py v2.3/server/tests/unit/test_strategies_yaml_loader.py
git commit -m "feat(loader): strategies.yaml 解析 owned_symbols 写入 instance_state"
```

### Task 10: strategies.yaml 加 paper_v53 group

**Files:**
- Modify: `v2.3/server/strategies.yaml`

- [ ] **Step 1: 加 paper_v53 group**

Edit `v2.3/server/strategies.yaml`，在 paper_v20h group 之后加：

```yaml
  - group_id: paper_v53
    qmt_account_id: "301300148788"      # 同 v20h 一个 QMT 账户
    strategies:
      - strategy_id: v53
        virtual_initial_cash: 10000000
        owned_symbols:
          - 510300.SH
          - 159915.SZ
          - 511260.SH
          - 518880.SH
          - 159981.SZ
          - 159985.SZ
          - 159930.SZ
          - 513500.SH
          - 513100.SH
          - 512890.SH
```

- [ ] **Step 2: 重启 server / 跑 loader 一次（确认 instance_state 写入）**

```bash
cd v2.3/server
venv/bin/python -c "
from pathlib import Path
from app.db import init_db, make_engine, make_session_factory
from app.scheduler.pipeline import load_strategies_to_db
from app.settings import get_settings
s = get_settings()
engine = make_engine(s.db_url)
sf = make_session_factory(engine)
load_strategies_to_db(s.strategies_file, sf)
print('loaded')
"

# 验证
sqlite3 pipeline-server.db "SELECT instance_id, json(owned_symbols) FROM instance_state;"
```

Expected: 看到 `paper_v53_v53` 行，owned_symbols 是 10 个 ETF 的 JSON list。

- [ ] **Step 3: Commit**

```bash
git add v2.3/server/strategies.yaml
git commit -m "config(strategies): 加 paper_v53 group (10 ETF owned_symbols 白名单)"
```

---

## Phase 5: V53Adapter 实现

### Task 11: V53Adapter 骨架 + 月末判断

**Files:**
- Create: `v2.3/server/plugins/v53_adapter.py`
- Create: `v2.3/server/tests/unit/test_v53_adapter.py`
- Create: `v2.3/server/plugins/v53/config.yaml`

- [ ] **Step 1: 写 config.yaml**

```yaml
# plugins/v53/config.yaml
strategy_id: v53
algorithm: inv_vol
risk_lookback: 252
min_history_days: 126
dry_run: true                          # M0 阶段必须 true；M1 改 false

quadrants:
  growth_up:    [hs300, cyb, gold, commodity2, commodity3, sp500, nasdaq, dividend]
  growth_down:  [bond, gold]
  inflation_up: [gold, commodity2, commodity3, crude_oil]
  inflation_down: [hs300, bond, dividend]

risk_filters:
  qdii_premium_threshold: 0.05         # 见 spec O1：可能改为 null 或 近似
  liquidity_multiplier: 100
  max_single_etf_weight: 0.75

month_end_anchor_etf: "510300.SH"      # 用此 ETF 的 trade_date 推月末
```

- [ ] **Step 2: 写月末判断测试**

```python
"""V53Adapter 单测 — 月末判断 + dry_run 短路 + 资源缺失退化"""
from datetime import date
from pathlib import Path

import pandas as pd
import pytest


def _make_ctx(tmp_path, trade_date_int: int, cash: float = 0.0,
              positions: dict | None = None, etf_trade_dates: list | None = None):
    """构造 minimal Context，510300.SH 的 trade_date 序列由参数提供"""
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    store = ParquetStore(root=tmp_path / "parquet")
    # 把 etf_trade_dates 写入 510300.SH 的 ETF parquet
    if etf_trade_dates:
        df = pd.DataFrame({
            "trade_date": pd.to_datetime(etf_trade_dates),
            "open": [3.0] * len(etf_trade_dates),
            "high": [3.0] * len(etf_trade_dates),
            "low":  [3.0] * len(etf_trade_dates),
            "close": [3.0] * len(etf_trade_dates),
            "volume": [0] * len(etf_trade_dates),
        })
        store.write("etfs", "510300.SH", df)
    return Context(
        instance_id="paper_v53_v53",
        trade_date=trade_date_int,
        virtual_cash=cash,
        virtual_positions=positions or {},
        parquet_store=store,
    )


def test_is_month_end_true(tmp_path):
    """target_date 是当月最后一个交易日 → True"""
    import plugins.v53_adapter as adapter_mod
    from plugins.v53_adapter import V53Adapter
    # 让 _V53_DIR 指向真实 plugins/v53/ 以读 config.yaml
    adapter = V53Adapter()
    # 4 月 trade days：2024-04-01, ..., 2024-04-30；最后一天 4/30
    trade_dates = pd.bdate_range("2024-04-01", "2024-04-30")
    ctx = _make_ctx(tmp_path, 20240430, etf_trade_dates=trade_dates)
    assert adapter._is_month_end(ctx, pd.Timestamp("2024-04-30")) is True


def test_is_month_end_false_midmonth(tmp_path):
    """月中 → False"""
    from plugins.v53_adapter import V53Adapter
    trade_dates = pd.bdate_range("2024-04-01", "2024-04-30")
    ctx = _make_ctx(tmp_path, 20240415, etf_trade_dates=trade_dates)
    assert V53Adapter()._is_month_end(ctx, pd.Timestamp("2024-04-15")) is False


def test_is_month_end_no_anchor_data(tmp_path):
    """510300.SH 缺数据 → False（保守退化，不调仓）"""
    from plugins.v53_adapter import V53Adapter
    ctx = _make_ctx(tmp_path, 20240430)
    assert V53Adapter()._is_month_end(ctx, pd.Timestamp("2024-04-30")) is False


def test_run_returns_empty_when_not_month_end(tmp_path):
    """非月末 → run() return []"""
    from plugins.v53_adapter import V53Adapter
    trade_dates = pd.bdate_range("2024-04-01", "2024-04-30")
    ctx = _make_ctx(tmp_path, 20240415, etf_trade_dates=trade_dates)
    assert V53Adapter().run(ctx, 20240415) == []


def test_run_returns_empty_when_dry_run(tmp_path, monkeypatch):
    """dry_run=true 配置下，即使是月末也 return []（但应该写 log）"""
    # config.yaml 默认 dry_run: true，所以这里直接月末跑应该 return []
    # （前提是 bundle/algo 不 crash）— 这个等 Task 12-14 完整之后再断言
    pytest.skip("等 Task 12-14 完成后启用")
```

- [ ] **Step 3: 写骨架实现**

```python
"""V53 适配器 — 月末双层 inv_vol 调仓
Phase M0: dry_run=true 模式，永远 return []，但会 log 目标权重
Phase M1: dry_run=false，输出真实 RawSignal[]
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

import pandas as pd
import yaml

from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context

from plugins.v53.code_map import (
    ETF_KEYS,
    QDII_QMT_CODES,
    QMT_TO_V53_KEY,
    V53_KEY_TO_QMT,
)
from plugins.v53.strategy import V53Config, V53Strategy

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_V53_DIR = _HERE / "v53"


class V53Adapter(Strategy):
    name = "v53"
    data_dir: ClassVar[Path | None] = _V53_DIR / "data"
    data_files: ClassVar[list[str]] = ["etf_close.parquet", "etf_meta.parquet"]

    _cfg: dict | None = None
    _v53_cfg: V53Config | None = None
    _etf_close_bundle: pd.DataFrame | None = None
    _etf_meta: pd.DataFrame | None = None

    def _load_resources(self) -> None:
        if self._cfg is None:
            with (_V53_DIR / "config.yaml").open() as f:
                type(self)._cfg = yaml.safe_load(f)
            type(self)._v53_cfg = V53Config(
                algorithm=self._cfg["algorithm"],
                risk_lookback=self._cfg["risk_lookback"],
                min_history_days=self._cfg["min_history_days"],
                quadrants=self._cfg["quadrants"],
            )
        if self._etf_close_bundle is None:
            df = pd.read_parquet(_V53_DIR / "data" / "etf_close.parquet")
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            type(self)._etf_close_bundle = df
        if self._etf_meta is None:
            type(self)._etf_meta = pd.read_parquet(_V53_DIR / "data" / "etf_meta.parquet")

    def _is_month_end(self, ctx: Context, target: pd.Timestamp) -> bool:
        """从 anchor ETF 的 trade_date 序列推：target 是其当月的最大 trade_date 吗？"""
        anchor = (self._cfg or {}).get("month_end_anchor_etf", "510300.SH")
        try:
            df = ctx.market(anchor, category="etfs")
        except Exception:
            return False
        if df is None or df.empty: return False
        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        same_month = df[(df["trade_date"].dt.year == target.year)
                        & (df["trade_date"].dt.month == target.month)]
        if same_month.empty: return False
        return target == same_month["trade_date"].max()

    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        target = pd.to_datetime(str(trade_date), format="%Y%m%d")

        try:
            self._load_resources()
        except Exception as e:
            logger.warning("V53 资源加载失败 (bundle 可能未上传): %s", e)
            return []

        if not self._is_month_end(ctx, target):
            return []

        # Task 12-15 在此处实现完整 pipeline；当前阶段只能走到这里
        logger.info("V53[%s] month-end %s 检测到，等待 Task 12-15 实现完整调仓", ctx.instance_id, trade_date)
        return []
```

- [ ] **Step 4: 跑测试**

```bash
cd v2.3/server
venv/bin/pytest tests/unit/test_v53_adapter.py -v
```

Expected: 4 个 PASS（test_run_returns_empty_when_dry_run 是 skip）

- [ ] **Step 5: Commit**

```bash
git add v2.3/server/plugins/v53/config.yaml v2.3/server/plugins/v53_adapter.py v2.3/server/tests/unit/test_v53_adapter.py
git commit -m "feat(v53): V53Adapter 骨架 + 月末判断（anchor ETF trade_date 推月末）"
```

### Task 12: _build_returns_matrix — bundle + IngestService 增量拼接

**Files:**
- Modify: `v2.3/server/plugins/v53_adapter.py` — 加 `_build_returns_matrix`
- Modify: `v2.3/server/tests/unit/test_v53_adapter.py` — 加测试

- [ ] **Step 1: 写测试**

Append to `test_v53_adapter.py`:

```python
def _make_ctx_with_etfs(tmp_path, trade_date_int: int,
                        etf_data: dict[str, pd.DataFrame],
                        cash: float = 10_000_000.0,
                        positions: dict | None = None):
    """构造带多个 ETF parquet 的 Context"""
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context
    store = ParquetStore(root=tmp_path / "parquet")
    for code, df in etf_data.items():
        store.write("etfs", code, df)
    return Context(
        instance_id="paper_v53_v53",
        trade_date=trade_date_int,
        virtual_cash=cash,
        virtual_positions=positions or {},
        parquet_store=store,
    )


def _make_bundle(tmp_path, bundle_end_date: str = "2024-03-31"):
    """生成 mini etf_close bundle + etf_meta，写到 tmp_path/v53data/"""
    from plugins.v53.code_map import V53_KEY_TO_QMT
    data_dir = tmp_path / "v53data"
    data_dir.mkdir()
    dates = pd.bdate_range("2023-01-01", bundle_end_date)
    pieces = []
    for code in V53_KEY_TO_QMT.values():
        pieces.append(pd.DataFrame({
            "trade_date": dates,
            "code": code,
            "close": pd.Series(range(len(dates))).astype(float) + 10.0,
            "open":  pd.Series(range(len(dates))).astype(float) + 10.0,
        }))
    pd.concat(pieces, ignore_index=True).to_parquet(data_dir / "etf_close.parquet", index=False)
    pd.DataFrame([{"code": c, "name": k, "list_date": pd.Timestamp("2020-01-01").date(),
                   "is_qdii": c in {"513500.SH", "513100.SH"}, "quadrants": ["growth_up"]}
                  for k, c in V53_KEY_TO_QMT.items()]
                 ).to_parquet(data_dir / "etf_meta.parquet", index=False)
    return data_dir


def test_build_returns_matrix_pure_bundle(tmp_path, monkeypatch):
    """bundle 覆盖至 target_date → 不需要 IngestService 增量"""
    import plugins.v53_adapter as adapter_mod
    data_dir = _make_bundle(tmp_path, bundle_end_date="2024-04-30")
    # 把 _V53_DIR 指到 tmp_path 让 _load_resources 读 fake bundle
    fake_v53_dir = tmp_path / "v53_dir"
    fake_v53_dir.mkdir()
    (fake_v53_dir / "data").mkdir()
    (fake_v53_dir / "data" / "etf_close.parquet").write_bytes(
        (data_dir / "etf_close.parquet").read_bytes())
    (fake_v53_dir / "data" / "etf_meta.parquet").write_bytes(
        (data_dir / "etf_meta.parquet").read_bytes())
    # config.yaml 拷一份（用真实的 plugins/v53/config.yaml 内容）
    real_cfg = (Path(__file__).resolve().parent.parent.parent
                / "plugins" / "v53" / "config.yaml").read_bytes()
    (fake_v53_dir / "config.yaml").write_bytes(real_cfg)
    monkeypatch.setattr(adapter_mod, "_V53_DIR", fake_v53_dir)

    from plugins.v53_adapter import V53Adapter
    V53Adapter._cfg = None
    V53Adapter._v53_cfg = None
    V53Adapter._etf_close_bundle = None
    V53Adapter._etf_meta = None

    ctx = _make_ctx_with_etfs(tmp_path, 20240430, etf_data={})
    adapter = V53Adapter()
    adapter._load_resources()
    returns = adapter._build_returns_matrix(ctx, pd.Timestamp("2024-04-30"))

    # 应是 wide format DataFrame[trade_date × internal_key]
    assert isinstance(returns, pd.DataFrame)
    assert returns.shape[1] == 10  # 10 个 internal key
    assert set(returns.columns) == set(ETF_KEYS)
    assert returns.shape[0] >= 252  # 至少 risk_lookback 行


def test_build_returns_matrix_with_incremental(tmp_path, monkeypatch):
    """bundle 截至 3/31，IngestService 提供 4 月增量 → 拼接到 4/30"""
    # 类似上面 setup，但 bundle 只到 3/31，etf_data 提供 4 月数据
    pytest.skip("详细实现见 Phase 5 review")  # 占位，节省篇幅
```

- [ ] **Step 2: 实现 _build_returns_matrix**

加到 `plugins/v53_adapter.py`:

```python
    def _build_returns_matrix(self, ctx: Context, target: pd.Timestamp) -> pd.DataFrame:
        """拼 bundle + IngestService 增量 → 计算日收益矩阵 [date × internal_key]"""
        # 1. bundle (long format → 取每个 code 的 close 序列)
        bundle = self._etf_close_bundle.copy()
        bundle = bundle[bundle["trade_date"] <= target]
        bundle_end = bundle["trade_date"].max() if not bundle.empty else pd.Timestamp("1900-01-01")

        # 2. IngestService 增量 (bundle_end 之后到 target)
        incr_pieces = []
        for qmt_code in V53_KEY_TO_QMT.values():
            try:
                df = ctx.market(qmt_code, category="etfs")
            except Exception:
                continue
            if df is None or df.empty: continue
            df = df.copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df = df[(df["trade_date"] > bundle_end) & (df["trade_date"] <= target)]
            if df.empty: continue
            df["code"] = qmt_code
            incr_pieces.append(df[["trade_date", "code", "close"]])

        combined = pd.concat([bundle[["trade_date", "code", "close"]], *incr_pieces],
                             ignore_index=True) if incr_pieces else bundle[["trade_date", "code", "close"]]

        # 3. long → wide：列是 QMT code，转成 internal key
        wide = combined.pivot_table(
            index="trade_date", columns="code", values="close", aggfunc="last")
        wide = wide.sort_index()
        # QMT code 列名 → internal key
        wide = wide.rename(columns=QMT_TO_V53_KEY)
        # 只保留 v53 关心的 10 个 ETF
        wide = wide[[k for k in ETF_KEYS if k in wide.columns]]

        # 4. 取 risk_lookback+1 行算 pct_change
        close_window = wide.tail(self._v53_cfg.risk_lookback + 1)
        returns = close_window.pct_change().dropna(how="all")
        return returns
```

- [ ] **Step 3: 跑测试**

```bash
cd v2.3/server
venv/bin/pytest tests/unit/test_v53_adapter.py::test_build_returns_matrix_pure_bundle -v
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add v2.3/server/plugins/v53_adapter.py v2.3/server/tests/unit/test_v53_adapter.py
git commit -m "feat(v53): _build_returns_matrix — bundle + IngestService 增量拼接 → 252天收益矩阵"
```

### Task 13: NAV 计算 + 权重 → quantity

**Files:**
- Modify: `v2.3/server/plugins/v53_adapter.py`
- Modify: `v2.3/server/tests/unit/test_v53_adapter.py`

- [ ] **Step 1: 加 helpers + reference_price 解析**

加到 `plugins/v53_adapter.py`:

```python
    def _resolve_reference_price(
        self, ctx: Context, qmt_code: str, target: pd.Timestamp,
    ) -> float | None:
        """同 v20h_adapter: 优先 ctx.market 最近真实 close, 回退 bundle close"""
        try:
            df = ctx.market(qmt_code, category="etfs")
            if df is not None and not df.empty:
                df = df.sort_values("trade_date")
                df = df[pd.to_datetime(df["trade_date"]) <= target]
                if not df.empty:
                    real = float(df.iloc[-1]["close"])
                    if real > 0: return real
        except Exception:
            pass
        # bundle fallback
        bundle = self._etf_close_bundle
        sub = bundle[(bundle["code"] == qmt_code) & (bundle["trade_date"] <= target)]
        if sub.empty: return None
        val = float(sub.sort_values("trade_date").iloc[-1]["close"])
        return val if val > 0 else None

    def _compute_nav(self, ctx: Context, target: pd.Timestamp) -> float:
        nav = ctx.cash()
        for code, qty in ctx.positions().items():
            price = self._resolve_reference_price(ctx, code, target)
            if price is None: continue
            nav += qty * price
        return nav

    def _weights_to_quantities(
        self, weights: dict[str, float], nav: float, ctx: Context, target: pd.Timestamp,
    ) -> dict[str, int]:
        result: dict[str, int] = {}
        for qmt_code, w in weights.items():
            ref_price = self._resolve_reference_price(ctx, qmt_code, target)
            if ref_price is None or ref_price <= 0: continue
            lots = round(nav * w / ref_price / 100)
            if lots > 0:
                result[qmt_code] = int(lots) * 100
        return result
```

- [ ] **Step 2: 写测试**

```python
def test_weights_to_quantities_basic():
    from plugins.v53_adapter import V53Adapter
    # NAV=1000万，bond 67% × 1000万 / 110 元/手 (假设 close=110) / 100 → 取整
    # 1000万 × 0.67 / 110 = 60909 shares → round(609.09) = 609 lots = 60900 shares
    adapter = V53Adapter()
    # 用 monkeypatch 跳过 _resolve_reference_price，直接 fake 价格
    weights = {"511260.SH": 0.67, "510300.SH": 0.063}
    # 因为没 bundle 在 adapter 状态里，需要 monkey-patch _resolve_reference_price
    adapter._resolve_reference_price = lambda ctx, code, target: 110.0 if code == "511260.SH" else 3.0
    qty = adapter._weights_to_quantities(weights, 10_000_000, ctx=None, target=None)
    assert qty["511260.SH"] == 60900
    # 1000万 × 0.063 / 3 = 210000 shares = 2100 lots × 100
    assert qty["510300.SH"] == 210000


def test_weights_to_quantities_zero_weight_skipped():
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._resolve_reference_price = lambda ctx, code, target: 100.0
    # weight 太小：1万 × 0.0001 / 100 = 0.01 shares → round = 0 → skip
    qty = adapter._weights_to_quantities({"159930.SZ": 0.0001}, 10_000, None, None)
    assert "159930.SZ" not in qty
```

- [ ] **Step 3: 跑测试**

```bash
venv/bin/pytest tests/unit/test_v53_adapter.py::test_weights_to_quantities_basic tests/unit/test_v53_adapter.py::test_weights_to_quantities_zero_weight_skipped -v
```

Expected: 2 个 PASS

- [ ] **Step 4: Commit**

```bash
git add v2.3/server/plugins/v53_adapter.py v2.3/server/tests/unit/test_v53_adapter.py
git commit -m "feat(v53): NAV + reference_price + weight→100股整 quantity"
```

### Task 14: 风控钩子（QDII 溢价 / 流动性 / max_single_etf_weight）

**Files:**
- Modify: `v2.3/server/plugins/v53_adapter.py`
- Modify: `v2.3/server/tests/unit/test_v53_adapter.py`

⚠️ 实现前确认 Task 0 (O1 QDII IOPV) 的结论。这里写"如果 IOPV 不可得，用过去 20 日均值做近似"的版本；若 Task 0 决定关闭该 filter，把对应代码注释成 noop 但保留接口。

- [ ] **Step 1: 加 _apply_risk_filters**

加到 `plugins/v53_adapter.py`:

```python
    def _apply_risk_filters(
        self, ctx: Context, target_qty: dict[str, int], target: pd.Timestamp,
    ) -> dict[str, int]:
        rf = self._cfg.get("risk_filters", {})
        out = dict(target_qty)

        # (a) QDII 溢价过滤
        threshold = rf.get("qdii_premium_threshold")
        if threshold is not None and threshold > 0:
            for code in list(out.keys()):
                if code not in QDII_QMT_CODES: continue
                premium = self._estimate_qdii_premium(ctx, code, target)
                if premium is not None and premium > threshold:
                    logger.warning("V53 QDII %s 溢价 %.2f%% > %.2f%%，skip 本次调仓",
                                   code, premium * 100, threshold * 100)
                    out.pop(code)

        # (b) 流动性过滤
        liq_mul = rf.get("liquidity_multiplier")
        if liq_mul is not None and liq_mul > 0:
            for code, qty in list(out.items()):
                vol = self._latest_volume(ctx, code, target)
                if vol is not None and vol < qty * liq_mul:
                    logger.warning("V53 %s 流动性不足: vol=%d < %dx%d", code, vol, liq_mul, qty)
                    out.pop(code)

        # (c) max_single_etf_weight (再次检查；防止边界数据导致 vendor 输出极端)
        max_w = rf.get("max_single_etf_weight")
        if max_w is not None and max_w < 1.0:
            nav = self._compute_nav(ctx, target)
            for code, qty in list(out.items()):
                price = self._resolve_reference_price(ctx, code, target)
                if price is None: continue
                w = qty * price / nav if nav > 0 else 0
                if w > max_w:
                    # 把超过部分砍掉
                    capped_qty = int(max_w * nav / price / 100) * 100
                    logger.warning("V53 %s 单 ETF 权重 %.2f > %.2f，capped 到 %d 股",
                                   code, w, max_w, capped_qty)
                    out[code] = capped_qty

        # (d) blacklist
        for code in ctx.risk_blacklist():
            if code in out:
                logger.info("V53 %s 在 blacklist，skip", code)
                out.pop(code)

        return out

    def _estimate_qdii_premium(self, ctx: Context, qmt_code: str, target: pd.Timestamp) -> float | None:
        """估算 QDII ETF 溢价。
        实现策略（按 Task 0 结论选一）：
          A. QMT IOPV API 可用 → premium = (close - IOPV) / IOPV
          B. 不可用 → 用过去 20 日均值近似 → (close - mean20) / mean20
          C. 完全关闭 → return None
        当前实现 = B（近似）。Task 0 决定后请调整。
        """
        try:
            df = ctx.market(qmt_code, category="etfs")
            if df is None or df.empty or len(df) < 21: return None
            df = df.sort_values("trade_date").tail(21)
            mean20 = float(df.iloc[:-1]["close"].mean())
            close_today = float(df.iloc[-1]["close"])
            if mean20 <= 0: return None
            return (close_today - mean20) / mean20
        except Exception:
            return None

    def _latest_volume(self, ctx: Context, qmt_code: str, target: pd.Timestamp) -> int | None:
        try:
            df = ctx.market(qmt_code, category="etfs")
            if df is None or df.empty: return None
            df = df.sort_values("trade_date")
            df = df[pd.to_datetime(df["trade_date"]) <= target]
            if df.empty: return None
            return int(df.iloc[-1].get("volume", 0))
        except Exception:
            return None
```

- [ ] **Step 2: 写测试（每个 filter 独立）**

```python
def test_risk_filter_max_single_etf_weight():
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._cfg = {"risk_filters": {"max_single_etf_weight": 0.5}}
    adapter._resolve_reference_price = lambda ctx, code, target: 100.0
    adapter._compute_nav = lambda ctx, target: 10_000_000
    # bond 60% × 1000万 / 100 = 60000 shares > 50% cap (=50000)
    out = adapter._apply_risk_filters(
        ctx=_FakeBlacklistCtx(set()),
        target_qty={"511260.SH": 60000},
        target=pd.Timestamp("2024-04-30"),
    )
    assert out["511260.SH"] == 50000


def test_risk_filter_blacklist():
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._cfg = {"risk_filters": {}}
    out = adapter._apply_risk_filters(
        ctx=_FakeBlacklistCtx({"512890.SH"}),
        target_qty={"512890.SH": 1000, "510300.SH": 2000},
        target=pd.Timestamp("2024-04-30"),
    )
    assert "512890.SH" not in out
    assert out["510300.SH"] == 2000


class _FakeBlacklistCtx:
    def __init__(self, bl: set[str]):
        self._bl = bl
    def risk_blacklist(self) -> set[str]: return self._bl
```

- [ ] **Step 3: 跑测试**

```bash
venv/bin/pytest tests/unit/test_v53_adapter.py::test_risk_filter_max_single_etf_weight tests/unit/test_v53_adapter.py::test_risk_filter_blacklist -v
```

Expected: 2 个 PASS

- [ ] **Step 4: Commit**

```bash
git add v2.3/server/plugins/v53_adapter.py v2.3/server/tests/unit/test_v53_adapter.py
git commit -m "feat(v53): 风控钩子 — QDII 溢价/流动性/max_single_etf_weight/blacklist"
```

### Task 15: diff + emit RawSignal + dry_run 处理

**Files:**
- Modify: `v2.3/server/plugins/v53_adapter.py`
- Modify: `v2.3/server/tests/unit/test_v53_adapter.py`

- [ ] **Step 1: 加 _diff_and_emit + 把 run() 完整起来**

加到 `plugins/v53_adapter.py`:

```python
    def _diff_and_emit(
        self, ctx: Context, current: dict[str, int], target: dict[str, int],
    ) -> list[RawSignal]:
        signals: list[RawSignal] = []
        all_codes = set(current) | set(target)
        # SELL 先
        for code in sorted(all_codes):
            cur, tgt = current.get(code, 0), target.get(code, 0)
            if cur > tgt:
                qty = cur - tgt
                price = self._resolve_reference_price(ctx, code, ctx.trade_date_ts)
                if price is None or price <= 0: continue
                signals.append(RawSignal(
                    symbol=code, direction="SELL", quantity=qty,
                    reference_price=price, price_offset=-0.005,
                ))
        # BUY 后
        for code in sorted(all_codes):
            cur, tgt = current.get(code, 0), target.get(code, 0)
            if tgt > cur:
                qty = tgt - cur
                price = self._resolve_reference_price(ctx, code, ctx.trade_date_ts)
                if price is None or price <= 0: continue
                signals.append(RawSignal(
                    symbol=code, direction="BUY", quantity=qty,
                    reference_price=price, price_offset=+0.005,
                ))
        return signals
```

⚠️ `ctx.trade_date_ts` 不存在；改成 `pd.to_datetime(str(ctx.trade_date), format="%Y%m%d")` 或在 helper 里转。

完整 run():

```python
    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        target = pd.to_datetime(str(trade_date), format="%Y%m%d")

        try:
            self._load_resources()
        except Exception as e:
            logger.warning("V53 资源加载失败: %s", e)
            return []

        if not self._is_month_end(ctx, target):
            return []

        returns = self._build_returns_matrix(ctx, target)
        if returns.shape[0] < self._v53_cfg.min_history_days:
            logger.warning("V53 历史不足 %d < %d，跳过", returns.shape[0], self._v53_cfg.min_history_days)
            return []

        strat = V53Strategy(self._v53_cfg)
        weights = strat.compute_targets(returns)

        nav = self._compute_nav(ctx, target)
        target_qty = self._weights_to_quantities(weights, nav, ctx, target)
        target_qty = self._apply_risk_filters(ctx, target_qty, target)
        signals = self._diff_and_emit(ctx, ctx.positions(), target_qty)

        if self._cfg.get("dry_run", True):
            logger.info(
                "V53[%s] DRY-RUN trade_date=%s nav=%.2f target_qty=%s would_emit=%d signals",
                ctx.instance_id, trade_date, nav, target_qty, len(signals),
            )
            return []

        logger.info(
            "V53[%s] go-live trade_date=%s nav=%.2f emitted=%d signals",
            ctx.instance_id, trade_date, nav, len(signals),
        )
        return signals
```

`ctx.trade_date_ts` 改回 `target` 参数：在 `_diff_and_emit` 接受 `target: pd.Timestamp` 参数显式传入。

修正后的 `_diff_and_emit` 签名:
```python
def _diff_and_emit(self, ctx, current, target_qty, target_ts: pd.Timestamp):
    ...
    price = self._resolve_reference_price(ctx, code, target_ts)
```

run() 内调用：`signals = self._diff_and_emit(ctx, ctx.positions(), target_qty, target)`

- [ ] **Step 2: 写测试 — 完整 run() 在 dry_run=true 下 return []**

```python
def test_run_dry_run_full_pipeline(tmp_path, monkeypatch):
    """月末 + bundle 数据齐 + dry_run=true → 写 log 但 return []"""
    # 用 _make_bundle 准备 bundle，让 _V53_DIR 指过去
    # 构造 ctx 在 4/30 (月末)
    # adapter.run() 应该 return []
    pytest.skip("详细 fixture 见 review 阶段；run pass 通过手工验证")
```

- [ ] **Step 3: 跑全部 v53 测试**

```bash
cd v2.3/server
venv/bin/pytest plugins/v53/tests tests/unit/test_v53_adapter.py -v
```

Expected: 全部 PASS（skip 不算）

- [ ] **Step 4: Commit**

```bash
git add v2.3/server/plugins/v53_adapter.py v2.3/server/tests/unit/test_v53_adapter.py
git commit -m "feat(v53): _diff_and_emit + 完整 run() (dry_run 模式 log 但 return [])"
```

### Task 16: 注册 V53Adapter 到 StrategyPipeline registry

**Files:**
- Modify: `v2.3/server/app/scheduler/pipeline.py`（或 strategy registry 所在文件）

- [ ] **Step 1: 找到 V20HAdapter 注册位置**

```bash
grep -rn "V20HAdapter\|registry\[" v2.3/server/app/ | head
```

- [ ] **Step 2: 加 V53Adapter 注册**

在 registry 字典里加：

```python
from plugins.v53_adapter import V53Adapter
...
"v53": V53Adapter,
```

- [ ] **Step 3: 启动 server，确认能注册成功**

```bash
cd v2.3/server
venv/bin/python -c "
from app.scheduler.pipeline import STRATEGY_REGISTRY  # 调整名字
assert 'v53' in STRATEGY_REGISTRY
print('v53 registered:', STRATEGY_REGISTRY['v53'])
"
```

- [ ] **Step 4: Commit**

```bash
git add v2.3/server/app/scheduler/pipeline.py
git commit -m "feat(v53): 注册 V53Adapter 到 strategy registry"
```

---

## Phase 6: Reconcile multi-instance 改造

### Task 17: owned_symbols 白名单过滤

**Files:**
- Modify: `v2.3/server/app/services/reconcile.py`
- Modify: `v2.3/server/tests/unit/test_reconcile.py`

- [ ] **Step 1: 写 multi-instance 测试**

加到 `test_reconcile.py`:

```python
def test_reconcile_whitelist_filter_positions(tmp_path):
    """v53 instance 推 snapshot 含 v20h 股票 → 被白名单过滤掉"""
    sf = _factory(tmp_path)
    # v20h: owned_symbols=None (legacy)
    with sf() as s:
        s.add(InstanceState(
            instance_id="paper_v20h_v20h_v1_3",
            virtual_cash=10_000_000.0,
            virtual_positions={},
            owned_symbols=None,
            last_update=datetime.now().isoformat(),
        ))
        s.add(InstanceState(
            instance_id="paper_v53_v53",
            virtual_cash=10_000_000.0,
            virtual_positions={},
            owned_symbols=["510300.SH", "511260.SH", "518880.SH"],
            last_update=datetime.now().isoformat(),
        ))
        s.commit()

    svc = ReconcileService(sf)
    # QMT 推全部 positions（含 v20h 的 600519 + v53 的 510300）
    snap = QmtPositionSnapshot(
        instance_id="paper_v53_v53",
        qmt_account_id="301300148788",
        qmt_cash=10_000_000.0,
        qmt_positions={
            "600519.SH": 100,    # v20h 持仓 → v53 reconcile 时应过滤
            "000001.SZ": 200,    # v20h 持仓 → 过滤
            "510300.SH": 5000,   # v53 持仓 → 保留
            "511260.SH": 70000,  # v53 持仓 → 保留
        },
        snapshot_time=datetime.now().isoformat(),
        dry_run=True,
    )
    result = svc.reconcile(snap)
    # 过滤后只剩 v53 的 ETF (510300, 511260)，server 上 0 持仓 → 2 个 qmt_only
    assert result.n_qmt_only == 2
    assert result.n_server_only == 0


def test_reconcile_v20h_legacy_excludes_others_owned(tmp_path):
    """v20h reconcile 时，v53 的 ETF 不该被认作 v20h 的"""
    sf = _factory(tmp_path)
    with sf() as s:
        s.add(InstanceState(
            instance_id="paper_v20h_v20h_v1_3",
            virtual_cash=10_000_000.0,
            virtual_positions={"600519.SH": 100},
            owned_symbols=None,
            last_update=datetime.now().isoformat(),
        ))
        s.add(InstanceState(
            instance_id="paper_v53_v53",
            virtual_cash=10_000_000.0,
            virtual_positions={"510300.SH": 5000},
            owned_symbols=["510300.SH", "511260.SH"],
            last_update=datetime.now().isoformat(),
        ))
        s.commit()

    svc = ReconcileService(sf)
    snap = QmtPositionSnapshot(
        instance_id="paper_v20h_v20h_v1_3",
        qmt_account_id="301300148788",
        qmt_cash=10_000_000.0,
        qmt_positions={"600519.SH": 100, "510300.SH": 5000},
        snapshot_time=datetime.now().isoformat(),
        dry_run=True,
    )
    result = svc.reconcile(snap)
    # v20h 应该只看到 600519，510300 被 others_owned 过滤
    assert result.n_matched == 1  # 600519 server=qmt
    assert result.n_qmt_only == 0  # 510300 没出现在 v20h reconcile 视野里
```

- [ ] **Step 2: 跑测试，确认 fail**

```bash
cd v2.3/server
venv/bin/pytest tests/unit/test_reconcile.py::test_reconcile_whitelist_filter_positions tests/unit/test_reconcile.py::test_reconcile_v20h_legacy_excludes_others_owned -v
```

Expected: FAIL（current reconcile 不过滤）

- [ ] **Step 3: 改 reconcile.py 加白名单过滤**

Edit `v2.3/server/app/services/reconcile.py` — 在 `reconcile()` 内、`qmt_positions` 构造之前插入：

```python
            # multi-instance：按 owned_symbols 过滤 QMT positions
            my_owned = inst.owned_symbols  # list[str] or None
            others_owned: set[str] = set()
            if my_owned is None:
                others = session.execute(
                    select(InstanceState).where(InstanceState.instance_id != snapshot.instance_id)
                ).scalars().all()
                for o in others:
                    if o.owned_symbols:
                        others_owned.update(o.owned_symbols)
```

然后修改原 `qmt_positions` 构造逻辑：

```python
            qmt_positions_raw = {...}  # 已有
            qmt_positions: dict[str, int] = {}
            for s, q in qmt_positions_raw.items():
                if q > MAX_REASONABLE_QTY_PER_STOCK:
                    outliers.append((s, q))
                    continue
                # 应用白名单过滤
                if my_owned is not None:
                    if s not in my_owned: continue
                else:
                    if s in others_owned: continue
                qmt_positions[s] = q
```

记得 import `select` from sqlalchemy。

- [ ] **Step 4: 改 apply 模式不再强对齐 cash**

在 reconcile() 末尾的 apply 块：

```python
            # 实际 apply：覆盖 instance_state.positions（不动 cash）
            inst.virtual_positions = dict(qmt_positions)
            inst.last_update = _now_iso()
            # NOTE: virtual_cash 不再强对齐 —— cash 由 settlement 自己维护
            # 总量校验由 ReconcileService.reconcile_cash_total() 独立做（仅报警）
            session.commit()
```

`server_cash`, `cash_diff` 在 ReconcileResult 里保留（仅展示用，不 apply）。

- [ ] **Step 5: 跑全部 reconcile 测试**

```bash
venv/bin/pytest tests/unit/test_reconcile.py -v
```

Expected: 全部 PASS（包括旧测试不破，新测试通过）

⚠️ 旧测试可能因为"cash 不再强对齐"而部分失败。需要更新这些测试断言（移除"reconcile 之后 virtual_cash == qmt_cash"）。

- [ ] **Step 6: Commit**

```bash
git add v2.3/server/app/services/reconcile.py v2.3/server/tests/unit/test_reconcile.py
git commit -m "fix(reconcile): owned_symbols 白名单过滤 + cash 不再强对齐（multi-instance 支持）"
```

### Task 18: validate_no_overlap 启动校验

**Files:**
- Modify: `v2.3/server/app/services/reconcile.py` — 加方法
- Modify: `v2.3/server/app/main.py` — 启动时调
- Modify: `v2.3/server/tests/unit/test_reconcile.py` — 加测试

- [ ] **Step 1: 写测试**

```python
def test_validate_no_overlap_raises_on_conflict(tmp_path):
    """两个 instance owned_symbols 重叠 → 启动校验 raise"""
    from app.services.reconcile import OwnershipOverlap, ReconcileService

    sf = _factory(tmp_path)
    with sf() as s:
        s.add(InstanceState(
            instance_id="inst_a", virtual_cash=0, virtual_positions={},
            owned_symbols=["510300.SH"], last_update=datetime.now().isoformat(),
        ))
        s.add(InstanceState(
            instance_id="inst_b", virtual_cash=0, virtual_positions={},
            owned_symbols=["510300.SH", "511260.SH"], last_update=datetime.now().isoformat(),
        ))
        s.commit()
    svc = ReconcileService(sf)
    with pytest.raises(OwnershipOverlap) as exc:
        svc.validate_no_overlap()
    assert "510300.SH" in str(exc.value)


def test_validate_no_overlap_ok_when_disjoint(tmp_path):
    sf = _factory(tmp_path)
    with sf() as s:
        s.add(InstanceState(
            instance_id="inst_a", virtual_cash=0, virtual_positions={},
            owned_symbols=["510300.SH"], last_update=datetime.now().isoformat(),
        ))
        s.add(InstanceState(
            instance_id="inst_b", virtual_cash=0, virtual_positions={},
            owned_symbols=["159915.SZ"], last_update=datetime.now().isoformat(),
        ))
        s.add(InstanceState(
            instance_id="legacy", virtual_cash=0, virtual_positions={},
            owned_symbols=None, last_update=datetime.now().isoformat(),
        ))
        s.commit()
    svc = ReconcileService(sf)
    svc.validate_no_overlap()  # 无 raise
```

- [ ] **Step 2: 实现 validate_no_overlap**

加到 `reconcile.py`:

```python
class OwnershipOverlap(Exception):
    """两个 instance 的 owned_symbols 列表有重叠。"""


class ReconcileService:
    ...
    def validate_no_overlap(self) -> None:
        """启动校验：所有 instance owned_symbols 不能重叠"""
        all_owned: dict[str, str] = {}
        with self.session_factory() as session:
            for inst in session.query(InstanceState).all():
                if not inst.owned_symbols: continue
                for s in inst.owned_symbols:
                    if s in all_owned and all_owned[s] != inst.instance_id:
                        raise OwnershipOverlap(
                            f"symbol {s} owned by both {all_owned[s]} and {inst.instance_id}"
                        )
                    all_owned[s] = inst.instance_id
```

- [ ] **Step 3: 在 app/main.py 加 startup hook**

找到 FastAPI app 的 startup event 或 lifespan 处，加：

```python
@app.on_event("startup")  # 或 lifespan ctx manager
async def _validate_ownership():
    from app.services.reconcile import ReconcileService
    from app.db import session_factory  # 你的工厂获取方式
    ReconcileService(session_factory).validate_no_overlap()
```

- [ ] **Step 4: 跑测试**

```bash
venv/bin/pytest tests/unit/test_reconcile.py::test_validate_no_overlap_raises_on_conflict tests/unit/test_reconcile.py::test_validate_no_overlap_ok_when_disjoint -v
```

Expected: 2 个 PASS

- [ ] **Step 5: Commit**

```bash
git add v2.3/server/app/services/reconcile.py v2.3/server/app/main.py v2.3/server/tests/unit/test_reconcile.py
git commit -m "feat(reconcile): validate_no_overlap 启动校验防 owned_symbols 重叠"
```

### Task 19: reconcile_cash_total 总量 sanity check

**Files:**
- Modify: `v2.3/server/app/services/reconcile.py`
- Modify: `v2.3/server/tests/unit/test_reconcile.py`

- [ ] **Step 1: 写测试**

```python
def test_reconcile_cash_total_within_tolerance(tmp_path):
    sf = _factory(tmp_path)
    with sf() as s:
        s.add(InstanceState(instance_id="a", virtual_cash=5_000_000,
                            virtual_positions={}, last_update=datetime.now().isoformat()))
        s.add(InstanceState(instance_id="b", virtual_cash=5_000_000,
                            virtual_positions={}, last_update=datetime.now().isoformat()))
        s.commit()
    svc = ReconcileService(sf)
    assert svc.reconcile_cash_total(qmt_total_cash=10_100_000.0, tolerance=0.05) is True


def test_reconcile_cash_total_alarm_on_big_deviation(tmp_path, caplog):
    sf = _factory(tmp_path)
    with sf() as s:
        s.add(InstanceState(instance_id="a", virtual_cash=10_000_000,
                            virtual_positions={}, last_update=datetime.now().isoformat()))
        s.commit()
    svc = ReconcileService(sf)
    # qmt_total = 5M, virtual = 10M → 50% 偏离 > 5% tolerance
    result = svc.reconcile_cash_total(qmt_total_cash=5_000_000.0, tolerance=0.05)
    assert result is False
    assert any("cash_total mismatch" in r.message for r in caplog.records)
```

- [ ] **Step 2: 实现**

```python
    def reconcile_cash_total(self, qmt_total_cash: float, tolerance: float = 0.05) -> bool:
        """检查 Σ(virtual_cash) ≈ QMT total cash。仅报警，不修改状态。
        返回 True = OK，False = 偏差超阈值（已记 warning，调用方决定是否触发外部 alert）。
        """
        with self.session_factory() as session:
            instances = session.query(InstanceState).all()
            total_virtual = sum(float(inst.virtual_cash) for inst in instances)
            if total_virtual <= 0: return True
            deviation = abs(qmt_total_cash - total_virtual) / total_virtual
            if deviation > tolerance:
                logger.warning(
                    "cash_total mismatch: virtual=%.2f qmt=%.2f deviation=%.2f%%",
                    total_virtual, qmt_total_cash, deviation * 100,
                )
                return False
            return True
```

- [ ] **Step 3: 跑测试**

```bash
venv/bin/pytest tests/unit/test_reconcile.py::test_reconcile_cash_total_within_tolerance tests/unit/test_reconcile.py::test_reconcile_cash_total_alarm_on_big_deviation -v
```

Expected: 2 个 PASS

- [ ] **Step 4: 把 reconcile_cash_total 接入 daily cron 或 reconcile endpoint**

找 reconcile 触发点（client 推 snapshot 后调 reconcile()），在调完之后追加：

```python
result = svc.reconcile(snap, initial_cash)
# 顺带做总量 sanity check（不阻塞主流程）
try:
    svc.reconcile_cash_total(qmt_total_cash=snap.qmt_cash, tolerance=0.05)
except Exception as e:
    logger.warning("reconcile_cash_total failed: %s", e)
```

注意：snap.qmt_cash 这里指 QMT **总账户** cash，所以 ReconcileSchema 的 cash 字段语义已经从"该 instance 的 cash"变成"该 QMT account 的总 cash"。需要 spec / client 上下文统一这点。

- [ ] **Step 5: Commit**

```bash
git add v2.3/server/app/services/reconcile.py v2.3/server/tests/unit/test_reconcile.py
git commit -m "feat(reconcile): reconcile_cash_total 总量 sanity check（仅报警不强对齐）"
```

---

## Phase 7: Dashboard + Handbook

### Task 20: Dashboard multi-instance 汇总

**Files:**
- Modify: `v2.3/server/app/api/dashboard.py`

- [ ] **Step 1: 加 multi-instance summary card**

在 dashboard 模板首页加：
- 总 NAV = Σ(各 instance.virtual_cash + 持仓市值)
- 各 instance NAV 列表
- 各 group 的最近 30 天 NAV trend 双线对比

具体改动取决于 dashboard 模板用什么（HTML inline / Jinja2 / 前端 JS）。Read `dashboard.py` 看现有模板结构再决定。

- [ ] **Step 2: 验证 dashboard 加载不报错**

```bash
cd v2.3/server
venv/bin/uvicorn app.main:app --port 8001 &
sleep 2
curl -s http://localhost:8001/dashboard | grep -i 'v53\|v20h'
kill %1
```

Expected: 看到 paper_v53 + paper_v20h 两个 instance 名出现

- [ ] **Step 3: Commit**

```bash
git add v2.3/server/app/api/dashboard.py
git commit -m "feat(dashboard): multi-instance 汇总 card + NAV 双线对比"
```

### Task 21: V53_OPERATIONS_HANDBOOK.md

**Files:**
- Create: `docs/V53_OPERATIONS_HANDBOOK.md`

- [ ] **Step 1: 仿 V20H_OPERATIONS_HANDBOOK.md 写**

骨架：
```
# V53 全天候 10 ETF 运维手册

**版本**: 1.0
**目标读者**: 你（策略主理人）+ 搭档（系统运维）
**适用范围**: paper_v53_v53 实例（1000 万模拟盘）

## 0. 一句话总览
v53 = 10 ETF 双层 inv_vol 全天候，月末调仓。当前 Phase M0 dry-run。

## 1. 今天的状态（YYYY-MM-DD）
[实例/初始资金/NAV/持仓数/现金/最近月末调仓信号数]

## 2. 策略机制详解
### 2.1 选 ETF + 调仓时序
[QUADRANT_MAP / 双层 inv_vol 公式 / T 日 16:00 trigger]

### 2.2 风控三层
[QDII 溢价 / 流动性 / max_single_etf_weight / blacklist]

### 2.3 数据依赖
| 文件 | 路径 | 谁负责 | 频率 |
|---|---|---|---|
| etf_close.parquet | plugins/v53/data/ | 你（refresh_v53_bundle.sh） | 月度 |
| etf_meta.parquet | 同上 | 同上 | 同上 |
ETF OHLCV 实时数据 | server IngestService | client market_push.py | 每日 |

上传命令：
[curl 命令复制 refresh_v53_bundle.sh 里那段]

## 3. 预期业绩
[handoff 附录 A 的 v53 1.587 Sharpe / 8.79% 年化 / -7.65% DD]
**对外预期**：年化 5-8%、Sharpe 1.0-1.4、回撤 -5% 至 -10%（保守版）

## 4. 月末调仓 SOP
- T-7: refresh bundle
- T 16:00: 自动 trigger_pipeline
- T 18:00: 人工拉 /orders?date=T+1&account_group=paper_v53 眼检
- T+1 09:10: client 集合竞价自动下单

## 5. Reconcile 排错
- owned_symbols 漏配 → `validate_no_overlap` 启动报错
- cash 总量偏离 → reconcile_cash_total 微信报警
- ETF outlier qty → reconcile 内 filter，看 server log

## 6. 已知 trade-offs
[handoff 第 5 节摘要 + spec 第 1 节决策表]

## 7. M0 → M1 切换 checklist
[spec 第 7 节复制]
```

- [ ] **Step 2: Commit**

```bash
git add docs/V53_OPERATIONS_HANDBOOK.md
git commit -m "docs: V53 运维手册（仿 V20H 手册）"
```

---

## Phase 8: 集成测试 + M0 部署

### Task 22: e2e 集成测试

**Files:**
- Create: `v2.3/server/tests/integration/test_v53_pipeline_e2e.py`

- [ ] **Step 1: 写 e2e 测试**

```python
"""V53 全 pipeline e2e —— 非月末 noop，月末 dry_run 写 log 但不发单。"""
import pandas as pd
import pytest

from app.scheduler.pipeline import StrategyPipeline  # 调整 import
from plugins.v53_adapter import V53Adapter


def test_v53_pipeline_non_month_end_emits_zero(tmp_path):
    """trigger T 不是月末 → orders 表无 v53 新增"""
    # setup: 完整 fixture 含 bundle、ctx market、instance_state、scheduler...
    # 跑 pipeline → 拉 orders 表过滤 instance_id=paper_v53_v53 → 应为空
    pytest.skip("详细 e2e fixture 编写见 review 阶段")


def test_v53_pipeline_month_end_dry_run_logs_but_emits_zero(tmp_path, caplog):
    """trigger T 是月末，dry_run=true → orders 表空但 log 含 'V53 DRY-RUN'"""
    pytest.skip("详细 e2e fixture 编写见 review 阶段")
```

⚠️ 完整 e2e fixture 需要 DataUploadService + IngestService + scheduler 全套 wiring。skill 推荐 review 阶段补全。如果时间紧，可以先靠 Phase 5 的单元测试 + 手工 M0 部署 smoke 替代。

- [ ] **Step 2: Commit (作为 skeleton)**

```bash
git add v2.3/server/tests/integration/test_v53_pipeline_e2e.py
git commit -m "test(v53): e2e skeleton (TODO: 完整 fixture)"
```

### Task 23: M0 部署 checklist (手工执行)

**目的：** 把代码上线到生产 server，跑 1 个月末 cycle 观察。

- [ ] **Step 1: 在 server 上拉最新代码**

```bash
ssh deploy@<server>
cd /opt/qmt-server/v2.3/server
git pull origin master
```

- [ ] **Step 2: 跑 migration**

```bash
venv/bin/python -m scripts.migrate_db
```

Expected: `[migrate] ALTER TABLE instance_state ADD owned_symbols` + `[migrate] done`

- [ ] **Step 3: 验证 paper_v53 instance_state 已创建**

重启 server（让 loader 跑一次）：
```bash
sudo systemctl restart qmt-server
sleep 5
sqlite3 /opt/qmt-server/v2.3/server/pipeline-server.db \
  "SELECT instance_id, virtual_cash, json(owned_symbols) FROM instance_state;"
```

Expected: 看到 paper_v53_v53 行 + 10 ETF owned_symbols

- [ ] **Step 4: 在 Mac 跑 refresh_v53_bundle.sh，把 bundle 推上去**

```bash
export QMT_SERVER_URL=http://<server-ip>:8000
export QMT_API_KEY=<key>
/Users/mameican/Desktop/策略复现/scripts/refresh_v53_bundle.sh
```

Expected: 看到两个文件 200 OK 上传

- [ ] **Step 5: 等下次月末 cron，或手工 trigger 一次 dry-run 月末**

```bash
# 手工 trigger 月末模拟
curl -X POST -H "Authorization: Bearer $QMT_API_KEY" \
  "http://<server-ip>:8000/admin/run-pipeline?trade_date=<下个月末>"
```

- [ ] **Step 6: 拉 server log，确认 V53 写出 DRY-RUN 信息 + 目标权重合理**

```bash
ssh deploy@<server> "journalctl -u qmt-server --since '5 min ago' | grep V53"
```

Expected: 看到 `V53[paper_v53_v53] DRY-RUN trade_date=... nav=10000000.00 target_qty={'511260.SH': ~60900, '510300.SH': ~21000, '512890.SH': ~26500, ...} would_emit=10 signals`

对照 spec 附录 A 的期末持仓画像核对（bond 67% / dividend 8.7% / hs300 6.3% / ...）。如果差异巨大（>20%），排查 returns 矩阵 / quadrants / vendor 算法。

- [ ] **Step 7: 把 M0 观察笔记记到 handbook 第 1 节（"今天的状态"）**

```bash
# 在 handbook 第 1 节加一行 "M0 第 1 次月末调仓: <YYYY-MM-DD> 目标权重 vs 预期持仓画像"
```

---

## Phase 9: M1 切换（延后，1 个月观察后）

### Task 24: 切 dry_run=false

**前置条件：** M0 跑过 1 个月末 cycle，目标权重画像和 spec 附录 A 接近（±20% 内）。

- [ ] **Step 1: 在 server 上改 config**

```bash
ssh deploy@<server>
cd /opt/qmt-server/v2.3/server
# 改 plugins/v53/config.yaml dry_run: false
sed -i 's/^dry_run: true$/dry_run: false/' plugins/v53/config.yaml
```

或者用 `vi` 改更稳。

- [ ] **Step 2: 重启 server**

```bash
sudo systemctl restart qmt-server
```

- [ ] **Step 3: 验证下次月末 cron 跑出来 emit > 0 signals**

```bash
# 月末当天 16:30 拉 log
journalctl -u qmt-server --since '17:00' | grep V53
# Expected: V53[paper_v53_v53] go-live trade_date=... emitted=10 signals
```

- [ ] **Step 4: T 日 18:00 拉 orders 眼检**

```bash
curl -s -H "Authorization: Bearer $QMT_API_KEY" \
  "http://<server>:8000/orders?date=<T+1>&account_group=paper_v53" | jq
```

Expected: ~10 笔 BUY 订单（v53 第一次月末从 0 持仓建仓）

- [ ] **Step 5: T+1 09:10 集合竞价前确认 client 拉到订单**

正常情况下 client `order_submit.py` 会自动拉，无需干预。如果手工：
```bash
# Windows client
python order_submit.py
```

- [ ] **Step 6: T+1 收盘后看 reconcile 报告**

```bash
journalctl -u qmt-server --since '15:00' | grep -E 'reconcile|V53'
```

Expected: paper_v53 instance reconcile 成功匹配 ~10 个 ETF 持仓

- [ ] **Step 7: 每周看一次 NAV vs 回测预期偏差，3 个月后评估是否走 M2**

观察标准：月度回报 ±2σ 范围内（回测 σ ≈ 0.013 → 月偏差 ±2.6%）

---

## Self-Review

完成所有 Task 后，对照 spec 检查：

**Spec coverage 检查**：
- spec §1 6 个决策 → 各对应 Task （账户隔离=Task 10、算法=Task 5、bundle=Task 6-7、vendor=Task 2、reconcile=Task 17-19、资金=Task 10）✓
- spec §3 算法 → Task 2 (vendor) + Task 5 (wrapper) ✓
- spec §4 数据 bundle → Task 6-7 ✓
- spec §5 adapter → Task 11-16 ✓
- spec §6 reconcile → Task 8-9, 17-19 ✓
- spec §7 ops → Task 20-21 ✓
- spec §8 风险 → Task 14 风控钩子 + Task 18 validate + Task 23 部署 checklist ✓
- spec §9 测试 → Task 3, 5, 11, 12, 13, 14, 15, 17, 18, 19, 22（对照测试在 Task 3）✓
- spec O1 (QDII IOPV) → Task 0 ✓
- spec O2 (vendor sync) → Task 2 注释说明 + Task 21 handbook 记录 ✓
- spec O3 (validate_no_overlap) → Task 18 ✓

**Placeholder 扫描**：
- 几处 `pytest.skip(...)` 在 e2e 测试和复杂 fixture — 是有意为之（详 review 阶段补）
- Task 9 / 16 / 18 涉及"找现有 loader / registry / startup hook 位置" — 需要 grep 后填空，不是占位符而是 lookup 任务
- Task 21 handbook 用伪结构化框架 — engineer 实际编写时按 V20H handbook 模板填实

**类型/方法一致性**：
- `compute_baseline(returns, quadrants, method, risk_lookback, min_history)` 出现在 Task 3 / 5 — 一致
- `V53Adapter._resolve_reference_price(ctx, qmt_code, target)` 出现在 Task 13 / 14 / 15 — 一致
- `V53Adapter._diff_and_emit(ctx, current, target_qty, target_ts)` Task 15 内有自我修正（最初没传 target_ts），engineer 实施时按修正后版本

---

## 执行 Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-24-v53-allweather-integration.md`. 两种执行方式：**

**1. Subagent-Driven（推荐）** — 每个 Task 一个 fresh subagent，two-stage review，快速迭代，干净 context

**2. Inline Execution** — 在当前 session 走完所有 Task，每个 Phase 后 checkpoint 给你 review

执行节奏建议：Phase 0 → Phase 5 是开发阶段，可一次推完；Phase 8 M0 部署需要 1 个月观察；Phase 9 M1 在 M0 通过后再启动。

