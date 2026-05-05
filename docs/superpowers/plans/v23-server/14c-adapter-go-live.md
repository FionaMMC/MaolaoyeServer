# Plan 14c: V20H Adapter 切实盘 — 真实发出 RawSignal

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 把 V20H Adapter 从 dry-run（永远 return []）切换到生产模式 — 真的根据 V20H 的目标组合 vs 当前持仓 diff 输出 `RawSignal[]`，让 server 端 precheck → aggregate → orders 流水线接管。

**核心难点:**
1. **状态恢复（无持久化）**：V20H 内部有 `last_rb_idx`（上次调仓索引）等状态，这些原本在回测里是连续的；server 端每天独立调 `run()`，需要从其它来源重建。**采用纯函数式做法：从 strategies.yaml 的 `start_date` + 当前 trade_date 计算出 di（日期索引），不需要数据库持久化**。
2. **vol_scale 计算依赖 equity_history**：V20H 用最近 20 天的 equity 收益率算 vol。我们用 `PerfSnapshot` 表的 NAV 历史替代。
3. **目标组合 → 信号转换**：把 V20H 计算出的目标 positions 跟 ctx.positions() 做 diff，BUY 缺的，SELL 多的。
4. **期货部分继续 skip**：14c 只发股票信号；hedge 等 v2.4 server 端有 futures 支持后再开。

**Architecture:**
- 不改 v2.3 server 框架，只改 `plugins/v20h_adapter.py`
- 返回的 `RawSignal` 用 `reference_price=今日 close`，`price_offset=±0.005`
- 数量按 V20H 目标手数（已经是整百）

**Files:**
- `v2.3/server/plugins/v20h_adapter.py` (MODIFY)
- `v2.3/server/tests/unit/test_v20h_adapter.py` (MODIFY)
- `v2.3/server/tests/integration/test_v20h_pipeline_e2e.py` (NEW)

---

## Task 1: 改 V20HAdapter.run() 为实盘模式

主要改动两块：

### A. `_compute_di()` 新增辅助方法

```python
def _compute_di(self, trade_date: int) -> int:
    """根据 strategies.yaml 的 cfg.start_date 计算今日是第几个交易日（di）。

    用 pred_df 的 unique dates 作为交易日序列，避免再读交易日历。
    """
    target = pd.Timestamp(str(trade_date), format="%Y%m%d")
    all_dates = sorted(self._pred_df["date"].unique())
    for i, d in enumerate(all_dates):
        if d >= target:
            return i
    return len(all_dates) - 1
```

### B. run() 的最后一步：从 dry-run 切到 emit RawSignal

把现在的：

```python
# Phase 14a：永远返回空，不下单
return []
```

替换为：

```python
# Phase 14c：实盘 — 输出 RawSignal[]
signals: list[RawSignal] = []

# 先 SELL（卖出 V20H 不要的标的；含 close 全部）
for code6, qty in to_sell.items():
    qmt = _v20h_to_qmt_code(code6)
    price = prices_today.get(code6)
    if price is None or price <= 0:
        continue
    signals.append(RawSignal(
        symbol=qmt,
        direction="SELL",
        quantity=qty,
        reference_price=price,
        price_offset=-0.005,
    ))

for code6, qty in to_close.items():
    qmt = _v20h_to_qmt_code(code6)
    price = prices_today.get(code6)
    if price is None or price <= 0:
        continue
    signals.append(RawSignal(
        symbol=qmt,
        direction="SELL",
        quantity=qty,
        reference_price=price,
        price_offset=-0.005,
    ))

# 后 BUY
for code6, qty in to_buy.items():
    qmt = _v20h_to_qmt_code(code6)
    price = prices_today.get(code6)
    if price is None or price <= 0:
        continue
    signals.append(RawSignal(
        symbol=qmt,
        direction="BUY",
        quantity=qty,
        reference_price=price,
        price_offset=+0.005,
    ))

logger.info(
    "V20H[%s] go-live trade_date=%s emitted=%d (buy=%d sell=%d close=%d)",
    ctx.instance_id, trade_date,
    len(signals), len(to_buy), len(to_sell), len(to_close),
)
return signals
```

### C. `to_buy / to_sell / to_close` 的语义需要轻调

V20H 的 `rebalance_stocks(target_codes, vol_scale)` 内部已经做了"卖+买"，所以 `strategy.positions` 里反映的是**调仓后**的目标。我们 diff 的应该是 `调仓后 positions vs 调仓前 ctx.positions()`：

```python
# 之前 (Phase 14a)
target_positions = dict(strategy.positions)
before = ctx_positions
to_buy = {c: q for c, q in target_positions.items() if q > before.get(c, 0)}
to_sell = {c: before[c] - q for c, q in target_positions.items()
           if c in before and before[c] > q}
to_close = {c: before[c] for c in before if c not in target_positions}
```

这个语义在实盘下还是对的。**关键修复**：`to_buy[c]` 当前是「目标 - 已有」（增量），但 SELL 同样是增量。这是对的。

但 quantity 必须是 **100 整数倍**（A 股手数规则）。V20H 的 `rebalance_stocks` 已经按 `lot_size=100` 取整，所以 target_positions[c] 都是 100 倍数；diff 出来也是 100 倍数 ✓。

Edge case：`to_close` 是清仓，可能不是整百（V20H 之前部分卖出已凑不齐整数手）。**V20H 实盘里这部分不发生**，因为 V20H 的 rebalance 全卖时是整数手；但保险起见，要求 SELL 信号 quantity > 0 且 close 类的 < 100 走 precheck "清仓尾单" 豁免（已经在 PrecheckService 实现）✓。

---

## Task 2: 单测调整 + 增加正向用例

`tests/unit/test_v20h_adapter.py` 改造：

1. 删除 `test_adapter_returns_empty_in_dry_run_mode`（语义变了）
2. 新加 `test_adapter_emits_signals_when_data_ready`（用 mock 数据）
3. 新加 `test_adapter_returns_empty_on_missing_external_data`（保留：缺 pred 仍然空）
4. `test_code_conversion` + `test_adapter_class_attrs` 不变

新测试草稿：

```python
def test_adapter_emits_buy_signals_for_target_positions(tmp_path: Path, monkeypatch):
    """有 pred + v12 数据，且 ctx 有当日行情时，应输出 BUY RawSignals。"""
    import pandas as pd
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context
    from plugins.v20h_adapter import V20HAdapter

    # 1) 准备一个最小 mock 数据集
    fake_dir = tmp_path / "v20h_data"
    fake_dir.mkdir()

    # pred: 1 个日期、3 只股票，prob_top 都正
    pred = pd.DataFrame({
        "date": [pd.Timestamp("20240403")] * 3,
        "code": ["600519", "000001", "000002"],
        "close": [1500.0, 10.0, 8.0],
        "prob_top": [0.8, 0.7, 0.6],
        "excess_ret": [0.01, 0.005, 0.002],
    })
    pred.to_parquet(fake_dir / "pred_csi1000.parquet")

    # v12 = 0.5（中性），用日期索引
    v12 = pd.DataFrame({"exposure": [0.5]}, index=[pd.Timestamp("20240403")])
    v12.index.name = None
    v12.to_parquet(fake_dir / "v12_exp_hs300.parquet")

    # 2) 把 V20HAdapter 的 _V20H_DIR 临时指向 fake_dir 的父目录
    import plugins.v20h_adapter as adapter_mod
    monkeypatch.setattr(adapter_mod, "_V20H_DIR", fake_dir.parent)

    # 重置 adapter class 缓存
    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None

    # 还需要让 plugins/v20h/config.yaml 可读：fake_dir.parent 下放一个最小 config
    cfg_yaml = """
capital_init: 10_000_000
cut_pct: 0.10
rebal_freq: 42
weight_cap: 1.5
q10_quantile: 0.10
q20_quantile: 0.20
q40_quantile: 0.40
q_warmup_days: 1   # 测试用 1 天就够
use_vol_target: false
target_vol_ann: 0.15
vol_lookback: 20
stock_cmn_rate: 0.0003
min_stock_cmn: 5.0
stamp_duty: 0.0005
bond_yield: 0.035
fut_cmn_rate: 0.0005
basis_cost: 0.03
fut_margin_ratio: 0.15
roll_cost_bps: 10
lot_size: 100
cash_buffer: 0.02
start_date: "2024-04-03"
"""
    (fake_dir.parent / "config.yaml").write_text(cfg_yaml, encoding="utf-8")

    # 3) Build ctx + 给三只股票各推一条当日行情
    store = ParquetStore(root=tmp_path / "parquet")
    for symbol_qmt, close in [("600519.SH", 1500.0), ("000001.SZ", 10.0), ("000002.SZ", 8.0)]:
        store.append("stocks", symbol_qmt, pd.DataFrame([{
            "trade_date": 20240403, "open": close, "high": close,
            "low": close * 0.99, "close": close,
            "volume": 1000, "amount": close * 1000, "suspendFlag": 0,
        }]))
    store.append("indexes", "000852.SH", pd.DataFrame([{
        "trade_date": 20240403, "open": 6000, "high": 6010,
        "low": 5990, "close": 6005,
        "volume": 0, "amount": 0,
    }]))

    ctx = Context(
        instance_id="paper_v20h_v20h_v1_3",
        trade_date=20240403,
        virtual_cash=10_000_000.0,
        virtual_positions={},
        parquet_store=store,
    )

    # 4) 跑 adapter
    adapter = V20HAdapter()
    signals = adapter.run(ctx, 20240403)

    # V20H 第一天会调仓买入（di=0, last_rb_idx=-42 触发 rebalance）
    assert len(signals) > 0
    assert all(s.direction == "BUY" for s in signals)
    # 数量必须是 100 整数倍
    assert all(s.quantity % 100 == 0 for s in signals)
    assert all(s.price_offset == 0.005 for s in signals)


def test_adapter_returns_empty_on_missing_pred(tmp_path: Path, monkeypatch):
    """pred_csi1000 缺失 → 空 list。"""
    import plugins.v20h_adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "_V20H_DIR", tmp_path)
    adapter_mod.V20HAdapter._cfg = None
    adapter_mod.V20HAdapter._pred_df = None
    adapter_mod.V20HAdapter._v12_series = None

    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    store = ParquetStore(root=tmp_path / "parquet")
    ctx = Context(
        instance_id="x", trade_date=20240403,
        virtual_cash=0.0, virtual_positions={},
        parquet_store=store,
    )
    signals = adapter_mod.V20HAdapter().run(ctx, 20240403)
    assert signals == []
```

---

## Task 3: 集成测试 e2e

`tests/integration/__init__.py`:

```python
# 包标记
```

`tests/integration/test_v20h_pipeline_e2e.py`:

```python
"""V20H 完整管线 e2e: 推行情 → 上传外部数据 → run-pipeline → GET /orders 看到订单。"""
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

_AUTH = {"Authorization": "Bearer TEST_KEY"}


@pytest.fixture
def v20h_plugin_in_test_dir(settings_for_test):
    """在 settings 的 plugins_dir 下放一个最小 V20H 风格 plugin。"""
    plugins = settings_for_test.plugins_dir
    plugins.mkdir(parents=True, exist_ok=True)

    # 拷一个 V20H 类似的最小适配器：随便买一只股票
    plugin_code = '''
from pathlib import Path
from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context

class FakeV20H(Strategy):
    name = "fake_v20h"
    data_dir = Path(__file__).parent / "_fake_v20h_data"
    data_files = ["pred.parquet"]
    def run(self, ctx, trade_date):
        # 检查外部数据是否上传
        pred_path = self.data_dir / "pred.parquet"
        if not pred_path.exists():
            return []
        # 简单：买 600519 100 股
        df = ctx.market("600519.SH", fields=["close"])
        if df.empty:
            return []
        close = float(df["close"].iloc[-1])
        return [RawSignal(
            symbol="600519.SH", direction="BUY", quantity=100,
            reference_price=close, price_offset=0.005,
        )]
'''
    (plugins / "fake_v20h_adapter.py").write_text(plugin_code, encoding="utf-8")

    # strategies.yaml 配一个对应实例
    strategies_yaml = settings_for_test.strategies_file
    strategies_yaml.write_text('''
account_groups:
  - group_id: paper_test
    qmt_account_id: ""
    strategies:
      - strategy_id: fake_v20h
        virtual_initial_cash: 1000000
''', encoding="utf-8")

    # 重置缓存
    from app.dependencies import _strategy_registry
    _strategy_registry.cache_clear()

    yield


def test_full_pipeline_with_data_upload(client, settings_for_test,
                                         v20h_plugin_in_test_dir):
    """1. 推行情 → 2. 上传外部数据 → 3. run-pipeline → 4. GET /orders 应非空。"""
    import io

    # 1. 推行情
    r = client.post("/market-data", headers=_AUTH, json={
        "trade_date": "20240403",
        "stocks": [{
            "symbol": "600519.SH", "open": 1490, "high": 1510,
            "low": 1485, "close": 1500,
            "volume": 1000, "amount": 1500000, "is_suspended": False,
        }],
        "indexes": [], "etfs": [],
    })
    assert r.json()["code"] == 0

    # 2. 上传外部数据
    df = pd.DataFrame([{"date": "20240403", "value": 0.5}])
    buf = io.BytesIO()
    df.to_parquet(buf, engine="pyarrow", index=False)

    r = client.post(
        "/admin/upload-data?strategy=fake_v20h&filename=pred.parquet",
        headers=_AUTH,
        files={"file": ("pred.parquet", buf.getvalue(), "application/octet-stream")},
    )
    assert r.json()["code"] == 0

    # 3. 触发 pipeline
    r = client.post("/admin/run-pipeline?trade_date=20240403", headers=_AUTH)
    body = r.json()
    assert body["code"] == 0
    summary = body["data"]
    assert summary["instances"] == 1
    assert summary["signals"] >= 1
    assert summary["passed"] >= 1
    assert summary["orders"] >= 1

    # 4. GET /orders 拿到订单
    r = client.get("/orders?date=20240403", headers=_AUTH)
    body = r.json()
    assert body["code"] == 0
    orders = body["data"]["orders"]
    assert len(orders) == 1
    assert orders[0]["symbol"] == "600519.SH"
    assert orders[0]["direction"] == "BUY"
    assert orders[0]["quantity"] == 100
```

---

## 验证 + Commit

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest -v
# 期望: 159 (Plan 14b 之后) - 1 (deleted dry-run test) + 2 (new adapter tests) + 1 (e2e) = 161 PASS
```

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/plugins/v20h_adapter.py \
        v2.3/server/tests/unit/test_v20h_adapter.py \
        v2.3/server/tests/integration/__init__.py \
        v2.3/server/tests/integration/test_v20h_pipeline_e2e.py
git commit -m "feat(plugins): switch V20H adapter from dry-run to live signals (Plan 14c)"
```

---

## 收尾

- [ ] 161+ pytest PASS
- [ ] 1 commit
- [ ] V20HAdapter.run() 在数据 OK 时输出真实 RawSignal[]
- [ ] e2e 测试覆盖：推行情 → 上传数据 → 跑管线 → GET /orders 非空

---

## 完事后部署到 server

```bash
# 1. push 到 GitHub
git push origin master

# 2. 让搭档（或自己 ssh）拉到 server
ssh root@120.26.138.82 "cd /opt/qmt-server && sudo -u qmtserver git pull && sudo -u qmtserver /opt/qmt-server/venv/bin/pip install scipy statsmodels && sudo systemctl restart qmt-server"

# 3. 上传 V20H 历史数据（一次性 47 MB）
bash /Users/mameican/Desktop/server/v2.3/server/scripts/upload_v20h_data.sh

# 4. 远程触发 pipeline
curl -X POST -H "Authorization: Bearer pipeline-v23-shared-secret-2026" \
  "http://120.26.138.82:8000/admin/run-pipeline?trade_date=20240403"
# 期望 instances=2, signals>0, orders>0

# 5. 看 orders
curl -H "Authorization: Bearer pipeline-v23-shared-secret-2026" \
  "http://120.26.138.82:8000/orders?date=20240403" | python3 -m json.tool
```

---

## 已知限制（待 v2.4）

| 限制 | 影响 | 缓解 |
|---|---|---|
| 期货对冲跳过 | 真实 V20H α 从 +12.5% 退化到 +5% 左右；DD 放大 | v2.4 server 加 futures 后启用 |
| 状态无持久化 | last_rb_idx 从 start_date 推算；理论上 OK 但若 strategies.yaml 改 start_date 会触发全部重平 | 文档说明：strategies.yaml 的 start_date 一旦定下不要乱改 |
| 性能未优化 | 每次 run() 重读 1000+ 个 parquet；server 上每天可能耗时 30+ 秒 | 加 `LRUCache` 给 ctx.market()；或一次性预读 wide DataFrame |
| 全市场 vs CSI1000 | V20H 设计是 CSI1000 universe；adapter 现在用整个 ctx.universe()（全市场） | adapter 改成 `for c in pred_today["code"]` 已经隐式限定到 pred 给出的 1000 只 |
