# Plan 14a: V20H Adapter — 骨架 + 离线 dry-run（不下单）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
>
> **Phase 0 已完成**：原版 `verify.py` 跑通，α/Sharpe/MaxDD 三指标全部通过基线验证。

**Goal:** 把 V20H 策略包接到 v2.3 server 的 plugin 框架；adapter 能从 Context 重建 V20H 所需输入、调用其决策、输出"今日想要的目标组合"。Phase 14a 只 **dry-run**：日志打印 + 写一个 verify 脚本，不发 RawSignal。

**Architecture:**

```
v2.3/server/
├── plugins/
│   ├── v20h/                     ← vendor 自 /Users/mameican/Desktop/量化/v20h_strategy/src/
│   │   ├── __init__.py
│   │   ├── strategy.py           
│   │   ├── data_loader.py
│   │   ├── stats.py
│   │   ├── backtest.py
│   │   ├── config.yaml           ← 也搬过来（adapter 可读）
│   │   └── data/                 ← .gitignored，部署时 rsync 上去
│   │       ├── pred_csi1000.parquet     (26 MB)
│   │       ├── stock_close.parquet      (6 MB)  
│   │       ├── stock_returns.parquet    (14 MB) ← Phase 1 不用，回测才用
│   │       ├── index_csi1000.parquet    (136 KB)
│   │       └── v12_exp_hs300.parquet    (36 KB)
│   └── v20h_adapter.py           ← 接到我们 Strategy 接口
└── scripts/
    └── verify_v20h_in_server.py  ← 用 server 视角跑一遍 V20H，对比原 verify.py 数字
```

**4 个关键设计决策：**

| 决策 | 选项 | 选择 | 理由 |
|---|---|---|---|
| Vendor 还是 pip install | (a) 拷代码到 plugins/v20h/ (b) 把策略打成 wheel pip install | **(a) vendor** | V20H 不是公开 PyPI 包；vendor 简单，每次升级 cp 一遍 |
| 数据文件位置 | (a) plugins/v20h/data/ + .gitignore (b) git LFS (c) 部署时 rsync | **(a)** | 本地开发简单；deploy/README.md 已有 rsync 套路；47 MB 不值得 LFS 复杂度 |
| code 格式转换 (000012 ↔ 000012.SZ) | (a) 简单规则: 6→SH, 其他→SZ (b) 维护 mapping 表 | **(a) + 例外列表** | 99% 场景规则正确；少量例外（如 688 科创 也是 SH，已含在"6 开头"规则里）单独列；指数 000852 转成 000852.SH |
| V20H 内部状态恢复 | (a) 每次重建（无状态） (b) 持久化到 DB (c) Phase 1 dry-run 不持久化，每次从 ctx+PerfSnapshot 派生 | **(c)** | dry-run 不需要持久化；Phase 3 实盘再做 |

---

## Files

**新建（v2.3/server 内）:**
- `v2.3/server/plugins/v20h/__init__.py` (NEW, `# 包标记`)
- `v2.3/server/plugins/v20h/strategy.py` (NEW, vendored)
- `v2.3/server/plugins/v20h/data_loader.py` (NEW, vendored)
- `v2.3/server/plugins/v20h/stats.py` (NEW, vendored — 仅 verify 脚本用)
- `v2.3/server/plugins/v20h/backtest.py` (NEW, vendored — 仅 verify 脚本用)
- `v2.3/server/plugins/v20h/config.yaml` (NEW, vendored)
- `v2.3/server/plugins/v20h_adapter.py` (NEW, glue 代码)
- `v2.3/server/plugins/v20h/data/.gitkeep` (NEW, 占位让目录入 git)
- `v2.3/server/scripts/__init__.py` (NEW)
- `v2.3/server/scripts/verify_v20h_in_server.py` (NEW)
- `v2.3/server/tests/unit/test_v20h_adapter.py` (NEW)

**修改:**
- `v2.3/server/.gitignore` (MODIFY，加 `plugins/v20h/data/*.parquet`)

**Phase 14a 不动:**
- 期货部分（V20H 的 hedge 逻辑）→ 14d 后续
- 实盘 RawSignal 输出 → Phase 14c
- 外部数据上传 endpoint → Phase 14b

---

## Task 1: Vendor V20H code

```bash
# 在 Mac 上执行
cp /Users/mameican/Desktop/量化/v20h_strategy/src/strategy.py \
   /Users/mameican/Desktop/server/v2.3/server/plugins/v20h/

cp /Users/mameican/Desktop/量化/v20h_strategy/src/data_loader.py \
   /Users/mameican/Desktop/server/v2.3/server/plugins/v20h/

cp /Users/mameican/Desktop/量化/v20h_strategy/src/stats.py \
   /Users/mameican/Desktop/server/v2.3/server/plugins/v20h/

cp /Users/mameican/Desktop/量化/v20h_strategy/src/backtest.py \
   /Users/mameican/Desktop/server/v2.3/server/plugins/v20h/

cp /Users/mameican/Desktop/量化/v20h_strategy/config.yaml \
   /Users/mameican/Desktop/server/v2.3/server/plugins/v20h/

# 数据先拷一份到 plugins/v20h/data/（本地开发用）
mkdir -p /Users/mameican/Desktop/server/v2.3/server/plugins/v20h/data
cp /Users/mameican/Desktop/量化/v20h_strategy/data/*.parquet \
   /Users/mameican/Desktop/server/v2.3/server/plugins/v20h/data/

# 写 __init__.py
echo "# 包标记 — V20H vendored 策略代码" \
   > /Users/mameican/Desktop/server/v2.3/server/plugins/v20h/__init__.py

# .gitignore 加一行
echo "v2.3/server/plugins/v20h/data/*.parquet" >> /Users/mameican/Desktop/server/.gitignore
# 留一个 .gitkeep 让目录还在 git 里
touch /Users/mameican/Desktop/server/v2.3/server/plugins/v20h/data/.gitkeep
```

`backtest.py` 里有 `from .strategy import ...` 的相对 import，vendor 后还能用 ✓。
`stats.py` 自包含 ✓。

---

## Task 2: 写 verify_v20h_in_server.py（验证 vendored 代码没改坏）

**目的：在 server 的 venv 里跑一次 V20H 完整回测，stats 必须跟原 verify.py 数字一致。**

`v2.3/server/scripts/verify_v20h_in_server.py`:

```python
"""验证 vendored V20H 代码在 server venv 里能跑通且数字一致。

目标:
  α (excess_ann):  +12.50% (allow ±0.5%)
  Sharpe:           1.12   (allow ±0.05)
  Max DD:          -17.22% (allow ±1.0%)
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

# 让 plugins/ 可被 import
_HERE = Path(__file__).resolve().parent
_SERVER_ROOT = _HERE.parent
sys.path.insert(0, str(_SERVER_ROOT))

from plugins.v20h.strategy import StrategyConfig
from plugins.v20h.data_loader import DataLoader
from plugins.v20h.backtest import run_backtest


EXPECTED = {
    "excess_ann": (0.1250, 0.005),
    "sharpe":     (1.12,   0.05),
    "max_dd":     (-0.1722, 0.010),
}


def main() -> int:
    print("=" * 60)
    print("  V20H Verify (in v2.3 server venv)")
    print("=" * 60)

    cfg_path = _SERVER_ROOT / "plugins" / "v20h" / "config.yaml"
    with cfg_path.open() as f:
        cfg = StrategyConfig(**yaml.safe_load(f))

    data_dir = _SERVER_ROOT / "plugins" / "v20h" / "data"
    loader = DataLoader(data_dir)
    loader.verify()
    data = loader.load_all()

    print(f"\n  运行 1000 万资金回测...")
    result = run_backtest(data, cfg)
    stats = result["stats"]

    print(f"\n  α:        {stats['excess_ann']:>+7.2%}")
    print(f"  Sharpe:   {stats['sharpe']:>7.2f}")
    print(f"  Max DD:   {stats['max_dd']:>+7.2%}")

    all_pass = True
    for metric, (expected, tol) in EXPECTED.items():
        actual = stats[metric]
        diff = abs(actual - expected)
        ok = diff <= tol
        sym = "✅" if ok else "❌"
        print(f"  {sym} {metric}: expected {expected:+.4f}, got {actual:+.4f}")
        if not ok:
            all_pass = False

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
```

**跑一次确认 vendor 没改坏：**
```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pip install -q scipy statsmodels   # V20H 多用了这两个包
python scripts/verify_v20h_in_server.py
```

期望：3 项都 ✅，跟原版同样数字。

---

## Task 3: 写 v20h_adapter.py（核心，dry-run mode）

**契约:**
- `name = "v20h_v1_3"` （与 strategies.yaml 的 strategy_id 对齐）
- `run(ctx, trade_date) -> list[RawSignal]`
- 暂时**永远返回空 list**，但日志输出"今日想要持仓 N 只 / 想买 X 卖 Y / vol_scale=Z"
- 这样接进 server 框架后能看到决策但不会真发单

**核心算法（adapter 内部，每次 run() 调用）：**

```python
1. 加载 config.yaml + 外部数据 pred + v12（从 plugins/v20h/data/）
2. 从 ctx 拿当前 cash + positions
3. 把 ctx.market() 历史 reshape 成 V20H 的 wide close_df（仅最近 N 天）
4. 重新构造 V20HStrategy 实例（无状态，每天一遍）
5. 通过当前 trade_date 在 pred.dates 里找到 di（索引）+ 41 天前的 last_rb_idx 模拟
6. 调 strategy.step() 算今日动作
7. diff strategy.positions vs ctx.positions() → 应卖出 X / 应买入 Y
8. 日志输出每只股票的目标 vs 当前
9. **return []**（dry-run）
```

`v2.3/server/plugins/v20h_adapter.py`:

```python
"""V20H 策略 adapter：把 plugins/v20h/ 的 V20HStrategy 接到 v2.3 server 框架。

Phase 14a: dry-run mode — 日志输出今日决策，不发 RawSignal（永远返回空 list）。
Phase 14c: 实盘 mode — 输出真实 RawSignal[]，期货部分仍 skip 直到 v2.4。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import yaml

from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context

# vendored 策略代码
from plugins.v20h.strategy import StrategyConfig, V20HStrategy, compute_expanding_quantiles

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_V20H_DIR = _HERE / "v20h"


# ── code 格式转换 ─────────────────────────────────────────────────────
def _v20h_to_qmt_code(code6: str) -> str:
    """6 位无后缀 → QMT 格式，规则: 6 开头 .SH，其他 .SZ。"""
    if code6.startswith("6") or code6.startswith("9") or code6.startswith("688"):
        return f"{code6}.SH"
    return f"{code6}.SZ"


def _qmt_to_v20h_code(qmt: str) -> str:
    return qmt.split(".")[0]


class V20HAdapter(Strategy):
    """V20H v1.3 适配器 — Phase 14a dry-run。"""

    name = "v20h_v1_3"

    # 配置缓存（懒加载）
    _cfg: StrategyConfig | None = None
    _pred_df: pd.DataFrame | None = None
    _v12_series: pd.Series | None = None

    def _load_resources(self) -> None:
        """懒加载 config + 外部数据。失败则记日志后续 run() 返回空。"""
        if self._cfg is None:
            cfg_path = _V20H_DIR / "config.yaml"
            with cfg_path.open() as f:
                cfg_dict = yaml.safe_load(f)
            type(self)._cfg = StrategyConfig(**cfg_dict)

        if self._pred_df is None:
            pred_path = _V20H_DIR / "data" / "pred_csi1000.parquet"
            df = pd.read_parquet(pred_path)
            if not pd.api.types.is_datetime64_any_dtype(df["date"]):
                df["date"] = pd.to_datetime(df["date"])
            type(self)._pred_df = df

        if self._v12_series is None:
            v12_path = _V20H_DIR / "data" / "v12_exp_hs300.parquet"
            v12 = pd.read_parquet(v12_path).squeeze()
            v12.index = pd.to_datetime(v12.index)
            type(self)._v12_series = v12

    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        """Dry-run: 日志输出决策，返回空 list 不下单。"""
        try:
            self._load_resources()
        except Exception as e:
            logger.warning("V20H 资源加载失败（外部数据可能未上传）: %s", e)
            return []

        cfg = self._cfg
        pred_df = self._pred_df
        v12 = self._v12_series

        target_date = pd.Timestamp(str(trade_date), format="%Y%m%d")

        # 当日预测
        pred_today = pred_df[pred_df["date"] == target_date]
        if pred_today.empty:
            logger.warning("V20H pred_csi1000 无 %s 数据，跳过", trade_date)
            return []

        # ── 重建 V20H 状态（无状态版本：从 ctx 派生）──────────────
        # 把当前 ctx.positions(QMT 格式) 转成 V20H 6 位 code
        ctx_positions = {
            _qmt_to_v20h_code(qmt): qty
            for qmt, qty in ctx.positions().items()
        }

        strategy = V20HStrategy(cfg)
        strategy.cash = ctx.cash()
        strategy.positions = dict(ctx_positions)

        # 当日所有股票价格（reshape ctx market 数据为 dict）
        prices_today = self._build_prices_today(ctx, pred_today)
        if not prices_today:
            logger.warning("V20H 无可交易标的（行情缺失），跳过 %s", trade_date)
            return []

        # CSI1000 当日价
        cur_idx_price = self._read_index_close(ctx, "000852.SH", trade_date)

        # V12 + Q 阈值
        v12_val = float(v12.get(target_date, 0.5))
        q_thresh = compute_expanding_quantiles(
            v12, start=cfg.start_date,
            quantiles=[cfg.q10_quantile, cfg.q20_quantile, cfg.q40_quantile],
            warmup=cfg.q_warmup_days,
        )
        q10 = float(q_thresh[cfg.q10_quantile].get(target_date, 0.30))
        q20 = float(q_thresh[cfg.q20_quantile].get(target_date, 0.30))
        q40 = float(q_thresh[cfg.q40_quantile].get(target_date, 0.30))

        # di（日期索引）：pred 的所有 date 排序后取 target_date 的位置
        all_dates = sorted(pred_df["date"].unique())
        di = next((i for i, d in enumerate(all_dates) if d == target_date), len(all_dates))

        # 调 step()
        log_entry = strategy.step(
            date=target_date,
            prices_today=prices_today,
            cur_idx_price=cur_idx_price,
            prev_idx_price=cur_idx_price,  # Phase 14a 简化
            v12_val=v12_val,
            q10=q10, q20=q20, q40=q40,
            pred_today=pred_today,
            di=di,
            is_roll_day=False,  # Phase 14a 跳过 roll 日处理
        )

        # ── diff 目标组合 vs 当前 ────────────────────────────────────
        target_positions = dict(strategy.positions)
        before = ctx_positions
        to_buy = {c: q for c, q in target_positions.items() if q > before.get(c, 0)}
        to_sell = {c: before[c] - q for c, q in target_positions.items()
                   if c in before and before[c] > q}
        to_close = {c: before[c] for c in before if c not in target_positions}

        logger.info(
            "V20H[%s] dry-run trade_date=%s n_target=%d cash=%.0f "
            "buy=%d sell=%d close=%d v12=%.3f vol_scale=%.2f hedge_target=%.2f",
            ctx.instance_id, trade_date,
            len(target_positions), strategy.cash,
            len(to_buy), len(to_sell), len(to_close),
            v12_val, log_entry.get("vol_scale", 1.0),
            log_entry.get("target_hedge", 0.0),
        )

        # 详细日志（最多打印 5 条避免刷屏）
        for code, qty in list(to_buy.items())[:5]:
            logger.info("  BUY  %s qty=%d (price=%.2f)",
                        _v20h_to_qmt_code(code), qty,
                        prices_today.get(code, 0.0))
        for code, qty in list(to_close.items())[:5]:
            logger.info("  SELL %s qty=%d (close)",
                        _v20h_to_qmt_code(code), qty)

        # Phase 14a：永远返回空，不下单
        return []

    # ── 内部：行情读取/转换 ──────────────────────────────────────────
    def _build_prices_today(
        self, ctx: Context, pred_today: pd.DataFrame,
    ) -> dict[str, float]:
        """从 ctx.market() 拼出 {6位code: today_close} dict。"""
        prices = {}
        for code6 in pred_today["code"].unique():
            qmt = _v20h_to_qmt_code(code6)
            df = ctx.market(qmt, fields=["close"], category="stocks")
            if df.empty:
                continue
            prices[code6] = float(df["close"].iloc[-1])
        return prices

    def _read_index_close(
        self, ctx: Context, qmt_code: str, trade_date: int,
    ) -> float | None:
        df = ctx.market(qmt_code, fields=["close"], category="indexes")
        if df.empty:
            return None
        return float(df["close"].iloc[-1])
```

---

## Task 4: 单元测试

`v2.3/server/tests/unit/test_v20h_adapter.py`:

```python
"""V20H adapter 单元测试 — mock ctx + 准备 mini 数据"""
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

# 注意：测试 import adapter 需要 plugins/v20h/data/*.parquet 存在
# 否则跳过外部数据相关的测试


def test_code_conversion():
    from plugins.v20h_adapter import _v20h_to_qmt_code, _qmt_to_v20h_code
    assert _v20h_to_qmt_code("000012") == "000012.SZ"
    assert _v20h_to_qmt_code("600519") == "600519.SH"
    assert _v20h_to_qmt_code("688981") == "688981.SH"
    assert _qmt_to_v20h_code("000012.SZ") == "000012"
    assert _qmt_to_v20h_code("600519.SH") == "600519"


@pytest.mark.skipif(
    not (Path(__file__).parent.parent.parent / "plugins" / "v20h" / "data" /
         "pred_csi1000.parquet").exists(),
    reason="V20H 外部数据未上传，跳过依赖数据的测试"
)
def test_adapter_returns_empty_in_dry_run_mode(tmp_path):
    """有数据时也应该返回空 list（Phase 14a dry-run）。"""
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    store = ParquetStore(root=tmp_path)
    ctx = Context(
        instance_id="real_A_v20h",
        trade_date=20240403,   # pred_csi1000 数据范围内的日期
        virtual_cash=10_000_000.0,
        virtual_positions={},
        parquet_store=store,
    )
    adapter = V20HAdapter()
    signals = adapter.run(ctx, trade_date=20240403)
    # Phase 14a 永远返回空，不下单
    assert signals == []


def test_adapter_handles_missing_external_data():
    """外部数据缺失时应优雅退化，不 crash。"""
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        # 临时清空 _cfg 等类属性以触发懒加载（避免被前一个测试缓存）
        V20HAdapter._cfg = None
        V20HAdapter._pred_df = None
        V20HAdapter._v12_series = None

        # mock 外部数据路径不存在的场景：暂时把 adapter 模块的 _V20H_DIR 指到空目录
        import plugins.v20h_adapter as adapter_mod
        original_dir = adapter_mod._V20H_DIR
        adapter_mod._V20H_DIR = Path(tmp)

        try:
            store = ParquetStore(root=Path(tmp))
            ctx = Context(
                instance_id="x", trade_date=20260430,
                virtual_cash=0.0, virtual_positions={},
                parquet_store=store,
            )
            signals = V20HAdapter().run(ctx, 20260430)
            assert signals == []
        finally:
            adapter_mod._V20H_DIR = original_dir
            V20HAdapter._cfg = None
            V20HAdapter._pred_df = None
            V20HAdapter._v12_series = None


def test_adapter_class_attrs():
    from plugins.v20h_adapter import V20HAdapter
    assert V20HAdapter.name == "v20h_v1_3"
```

---

## Task 5: 把 V20H 加进 strategies.yaml 和验证插件加载

更新 `v2.3/server/strategies.yaml`：

```yaml
account_groups:
  - group_id: real_A
    qmt_account_id: "1234567890"
    strategies:
      - strategy_id: buy_on_dip_example   # 已有
        virtual_initial_cash: 500000

  - group_id: paper_v20h          # 新加：纯虚拟测试组
    qmt_account_id: ""             # 留空，dry-run 不下单
    strategies:
      - strategy_id: v20h_v1_3
        virtual_initial_cash: 10000000
```

启动 server 验证 plugin 自动注册：

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
QMT_API_KEY=DEV python -m app.main &
sleep 2
curl -s -H "Authorization: Bearer DEV" \
  "http://localhost:8000/admin/run-pipeline?trade_date=20240403"
# 期望日志: V20H[paper_v20h_v20h_v1_3] dry-run trade_date=20240403 ...
kill %1
```

---

## 验证 + Commit

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pip install -q scipy statsmodels      # V20H 依赖

# 1. vendored 代码原样可跑
python scripts/verify_v20h_in_server.py
# 期望: 3 项全 ✅

# 2. server 单测全过
pytest -v
# 期望: 142 (旧) + 3 (adapter) = 145 PASS（数据 fixture 缺时跳过 1 个）

# 3. server 启动后 admin/run-pipeline 能调到 V20H
# （上面 Task 5 的 curl）
```

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/plugins/v20h/ \
        v2.3/server/plugins/v20h_adapter.py \
        v2.3/server/scripts/__init__.py \
        v2.3/server/scripts/verify_v20h_in_server.py \
        v2.3/server/strategies.yaml \
        v2.3/server/tests/unit/test_v20h_adapter.py \
        .gitignore
git commit -m "feat(plugins): add V20H v1.3 adapter (Phase 14a dry-run)"
```

---

## 收尾

- [ ] `verify_v20h_in_server.py` 跑 1000 万回测 3 项指标全 ✅
- [ ] `pytest -v` 全绿
- [ ] `python -m app.main` + `curl /admin/run-pipeline` 能看到 `V20H[...] dry-run` 日志
- [ ] `git log` 显示 1 个 commit（可能要 2 个：vendor + adapter；可拆可不拆）

---

## 后续 plan

- **Phase 14b**: `POST /admin/upload-data?type=pred|v12&date=YYYYMMDD` endpoint，把 pred_csi1000 + v12 的最新数据由策略生成方上传到 server
- **Phase 14c**: 把 adapter 从 dry-run 切到实盘 — 真的 emit RawSignal[]，由 precheck/aggregate 流水线接管
- **Phase 14d (远期)**: v2.4 server 加 futures 账户组支持 → V20H 期货对冲部分激活
