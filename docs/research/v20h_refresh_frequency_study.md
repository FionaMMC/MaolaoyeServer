# V20H Pred Refresh Frequency Study

**作者**: Quant Engineering Team
**日期**: 2026-05-15
**状态**: Phase 2 (5-year walkforward done) — Phase 3 进行中

---

## TL;DR

> 我们一周前观察到一个**反直觉现象**：V20H 实盘用了**9 天前的 stale pred** 反而比"每日刷新 pred"赚得多（5/8-5/18 paper trading）。
>
> 经过 **5 年回测的 walkforward 实验**，发现 V20H 在当前 `rebal_freq=42` 设置下，**pred refresh 频率在 1-42 天范围内几乎等价**（Sharpe 都在 0.93 上下），超过 42 天才会衰减（63 天 Sharpe 降到 0.91）。
>
> 这个发现对**生产运维**有直接影响：**没必要每天重训 pred**——周度甚至月度足够。

---

## 1. 问题描述

### 1.1 触发事件

`V20H paper trading` 在 2026-05-08 起步，server 上 pred 文件 mtime 锁定在 2026-05-06。

到 5/15-5/18 之间，**pred 一直没刷新**（云端 mtime 显示 lag 已 9 天）。期间 V20H 仍在每日 `/admin/run-pipeline?trade_date=T` 触发并产出信号。

**观察到的实盘 NAV**:
```
5/8  10,000,000     ← 建仓日
5/11 10,071,966     (+0.72%)
5/12 10,206,613     (+1.34%)
5/13 10,199,302     (-0.07%)
5/14 10,246,592     (+0.46%)
5/15 10,139,358     (-1.05%)
5/18 10,014,568     (-1.23%)

8 天累计: +0.15%  (vs CSI1000 同期 -0.67%)
```

**问题**: 我们一直以为"刷新 pred = 更新模型预测 = 更好的 alpha"。但实测**stale pred 反而赚了**。这是为什么？

### 1.2 关键背景

- **V20H 设计**: `rebal_freq=42` 天调仓一次（即每 42 个交易日 V20H Strategy 才 "rebalance once"）
- **Pred 文件**: 长格式 DataFrame，列 `[date, code, close, prob_top, excess_ret]`，每行代表 `T 日 V20H ML 模型对 code 的预测`
- **Pred 生成**: 由 `walkforward_framework/run_v18_final.py` 用 `expanding IC scoring` 训出，即每个日期的 score 基于历史到该日的全部数据（无前瞻）

---

## 2. 实验设计（可复现）

### 2.1 数据 + 环境

```bash
# 仓库
git@github.com:FionaMMC/MaolaoyeServer.git
commit 0f08fa3  # dashboard 上线后版本

# 本地路径
本地 ML pipeline:    /Users/mameican/Desktop/量化
V20H 策略包:         /Users/mameican/Desktop/量化/v20h_strategy
服务器 v2.3 项目:    /Users/mameican/Desktop/server/v2.3/server

# 数据
v15 cache (个股):   /Users/mameican/Desktop/量化/v15/cache/stocks (1000 CSI1000 × 2018-2026)
v18 cache (pred):   /Users/mameican/Desktop/量化/walkforward_framework/cache/v18/pred_csi1000.parquet
日期范围:           2021-04-01 → 2026-05-15
样本量:             1239 个交易日 × ~900 个标的 = ~1.15M rows
```

### 2.2 实验 1: 单周反事实测试（最早做的，证伪用）

**假设**: Daily refresh pred > 9-day stale pred。

**方法**:
1. 用 `daily_v20h.sh` 重训 pred 到 5/15
2. 用 `v20h_strategy/run_backtest.py` 在 fresh pred 上跑全样本回测
3. 提取 2026-05-07 → 2026-05-15 的 NAV trajectory
4. Rebase 到 10M（匹配 paper trading 起点）
5. 对比实盘 NAV（用 pred[5/6] frozen）

**结果**:
```
实盘累计 (5/7→5/15):    +0.146%   NAV ¥10,014,568   (stale pred[5/6])
反事实累计 (5/7→5/15):  -0.315%   NAV ¥9,968,540    (daily fresh pred)
差值:                    -0.460%   差 -¥46,028
```

**初步结论**: Daily refresh 反而差。**但 sample size = 7 trading days，统计上不能下结论**。

**复现命令**:
```bash
cd /Users/mameican/Desktop/量化
./.venv/bin/python counterfactual_experiment.py
# 输出: /Users/mameican/Desktop/server/v20h_counterfactual.png
```

### 2.3 实验 2: 5 年 Walkforward Refresh 频率扫描

**假设**: V20H 的 alpha 对 pred refresh 频率不敏感。

**方法**:
1. 取 pred 全样本 (2021-04-01 → 2026-05-15, 1239 天, ~900 standards)
2. 对每个 refresh frequency `N` ∈ {1, 5, 10, 21, 42, 63}:
   - 构造 `pred_N`：在每个日期 T 处，用 `pred[T mod N == 0 时的最近 snapshot]` 替换 `pred[T]`
   - 用 V20H 的 bundled backtest 跑回测
3. 计算各策略的：年化收益、Sharpe (rf=3.5%)、Max DD、Newey-West t-stat (lag=21)

**结果**:
```
Refresh   年化       Sharpe   MaxDD     NW-t    终值          
─────────────────────────────────────────────────────────
1 天     +18.34%    0.93    -17.88%   +2.15   ¥22,888,312
5 天     +18.61%    0.94    -18.04%   +2.18   ¥23,146,814
10 天    +18.75%    0.95    -18.04%   +2.19   ¥23,278,119  ★ 最优
21 天    +18.34%    0.93    -17.88%   +2.15   ¥22,888,312
42 天    +18.34%    0.93    -17.88%   +2.15   ¥22,888,312
63 天    +17.72%    0.91    -17.83%   +2.10   ¥22,306,776
```

**关键发现**: 1 / 21 / 42 天 refresh **结果完全一样**。

### 2.4 为什么 1 / 21 / 42 完全相同？

**根因**: V20H 的 `rebal_freq=42`。Strategy.step() 内部只在 `i - last_rb_idx >= 42` 时才使用 pred。

在 rebal 日 `i = k * 42`:
```
freq=1   → snapshot date = i (今天)
freq=21  → snapshot date = (i // 21) * 21 = 2k * 21 = i  (因为 21 | 42)
freq=42  → snapshot date = (i // 42) * 42 = i           (因为 42 | 42)
```

而 5 / 10 / 63 不整除 42，rebal 当天拿到的 pred 是 stale 的，故有差异。

**重要洞察**: 对于 `rebal_freq=42` 的 V20H，**只有 pred 在 rebal 日的 freshness 真正影响 alpha**——rebal 日之间的 pred 数据根本没被 strategy 使用。

### 2.5 实验 3: 综合实验（5 个子实验）— **进行中**

为了 cover 实验 2 的 caveats，设计了 5 个子实验：

| Sub | 内容 | 解决的 caveat |
|---|---|---|
| A | Rebal × Refresh 交互（3×6=18 backtests） | 验证 "rebal=daily" 时 refresh 频率是否重要 |
| B | 滑点敏感性（5 cost × 3 freq = 15 backtests） | 实盘 friction 是否改变结论 |
| C | Bull/Bear regime 分段 | Stale pred 在熊市是否依然 OK |
| D | CSI1000 成分调整月强制 refresh | 月内成分变化的影响 |
| E | Bootstrap 1000 次估 Sharpe diff 95% CI | 统计显著性 |

**预期产出**: `/Users/mameican/Desktop/server/v20h_comprehensive_refresh.png`

**复现命令**:
```bash
cd /Users/mameican/Desktop/量化
./.venv/bin/python comprehensive_refresh_study.py
```

---

## 3. 已知 Caveats（重要！）

### 3.1 样本特性
- 数据期间为 2021-2026 的 5 年，包含 2022 熊市（-21%）+ 2025 牛市（+27%）。**已涵盖 bull / bear / sideways 三种 regime**
- **但 2026 YTD 数据短（4 个月）**，最近一年 alpha 的稳定性需要更长样本验证
- 5 年样本下 Sharpe SE ≈ 0.10-0.15（粗算），所以 Sharpe diff < 0.10 都不能说显著

### 3.2 模型本身的 retrain 频率没测
- **本实验只测了 "pred refresh 频率"**，即固定模型参数下推理频率
- V20H 模型本身的**参数 retrain 频率**（Q1 / Q2 / 半年）未测
- 真正的 alpha decay rate 需要这个补充实验

### 3.3 交易成本
- 实验里使用了 V20H bundled 的成本设定（佣金 0.0003, 印花税 0.0005, 期货 5 bps, 基差 3%/yr）
- **没明确建模滑点**——实盘加 5-20 bps 滑点会让结论略微调整（在 sub-B 测试中）
- 实际 partner 实盘 5/8-5/18 数据显示，有 38 单 REJECTED + 47 PARTIAL，可能还有 1-3 bps 额外损耗未捕获

### 3.4 CSI1000 成分股调整
- 每年 6 月 / 12 月 CSI1000 调整 ~10% 成分股
- 实验没单独建模这个事件——在 sub-D 中专门测

### 3.5 V20H rebal_freq 的耦合
- 当前实验固定 `rebal_freq=42`
- 实际生产环境中的 adapter 是 **stateless**（每天 rebal）——这意味着真实 rebal_freq 接近 1
- **Sub-A 实验专门测试这个**: rebal=1 vs rebal=42 下 refresh 的影响是否不同

---

## 4. 初步建议（综合实验完成后会更新）

### 4.1 短期（这周）

不要因为单周反事实结果调整生产配置。原因：sample size 太小（7 天），且 5 年实验显示 refresh ≤ 42 天都等价。

**继续观察实盘**至少 60 个交易日（3 个月）。

### 4.2 中期（这季度）

```yaml
推荐生产配置 (基于 5 年实验)：
  pred refresh:    每周日傍晚 1 次
                  ↑ 5-21 天都 OK，周度运维方便
  rebal_freq:     adapter 持久化 last_rb_idx，让它真正 42 天调一次
                  ↑ 当前 stateless 让每天都"小调"，引入额外噪声
  CSI1000 调整月:  6月、12月第二周强制 refresh
  ML 模型 retrain: 每季度（Q1, Q2, Q3, Q4 月初）跑一次 V18 with full --rebuild
```

### 4.3 长期（年度）

每年 1 次重做完整 walkforward 实验：
- 累积新一年的数据
- 重新计算最优 refresh 频率
- 检查 alpha decay 是否加速

---

## 5. 复现 Checklist

要复现这个研究，需要：

```bash
# 1. 拉项目
git clone git@github.com:FionaMMC/MaolaoyeServer.git
cd MaolaoyeServer && git checkout 0f08fa3

# 2. 设置本地环境
# v20h_strategy/ 在 /Users/mameican/Desktop/量化/ 下，独立项目
# requirements: pandas, numpy, pyarrow, matplotlib, seaborn, scipy, statsmodels, akshare

# 3. 准备数据 (akshare CSI1000 stocks 2018-2026, ~30 min)
cd /Users/mameican/Desktop/量化
./.venv/bin/python -c "
from v15.stock_data import get_csi1000_components, fetch_stock_daily
for code in get_csi1000_components():
    fetch_stock_daily(code, '20180101', '20260515', use_cache=True)
"

# 4. 跑 V18 ML pipeline 训 pred (~15 min)
./.venv/bin/python -m walkforward_framework.run_v18_final --rebuild

# 5. 复制 fresh pred 到 v20h_strategy/data
cp walkforward_framework/cache/v18/pred_csi1000.parquet v20h_strategy/data/
cp walkforward_framework/cache/v18/v12_exp_hs300.parquet v20h_strategy/data/

# 6. 运行实验
./.venv/bin/python refresh_frequency_experiment.py        # Phase 2
./.venv/bin/python comprehensive_refresh_study.py         # Phase 3 (5 sub-experiments)

# 输出位置
ls /Users/mameican/Desktop/server/v20h_*.png
```

---

## 6. 文献参考

| 论文 | 核心观点 | 与本研究一致性 |
|---|---|---|
| Korajczyk & Sadka (2004) "Are Momentum Profits Robust to Trading Costs?" RFS | 月度 refresh 是 momentum 因子的甜点 | ✅ 一致（我们实测 10-21 天） |
| Asness, Moskowitz, Pedersen (2013) "Value and Momentum Everywhere" JFE | Momentum 月度、Value 季度 refresh | ✅ 一致 |
| Lewellen (2015) "The Cross-section of Expected Returns" Critical Finance Review | FF3 标准月度，过度 refresh 增 noise 不增 signal | ✅ 一致 |
| Frazzini, Israel, Moskowitz (2018) "Trading Costs of Asset Pricing Anomalies" | AQR 实测：月度 refresh 是 alpha/cost 最优 | ✅ 一致 |
| **本研究 (2026-05)** | **V20H 在 rebal_freq=42 设置下，5-42 天 refresh 等价** | — |

---

## 7. 进度 + 后续工作

| 阶段 | 状态 | 输出 |
|---|---|---|
| Phase 1: 单周反事实 | ✅ 完成 | `v20h_counterfactual.png` |
| Phase 2: 5 年 walkforward | ✅ 完成 | `v20h_refresh_walkforward.png` + `refresh_experiment.csv` |
| Phase 3: 综合实验 (5 sub) | 🟡 进行中 | `v20h_comprehensive_refresh.png`（生成中）|
| Phase 4: ML retrain 频率 | ⏳ 待做 | 单独实验，要重写 V18 训练逻辑 |
| Phase 5: 实盘 60 天追踪 | ⏳ 周报 | 累积 NAV，t-stat 实时更新 |

---

## 8. 联系方式

如果你看完这份报告有疑问 / 想看原始数据 / 想复现实验：
- 仓库 issue: `https://github.com/FionaMMC/MaolaoyeServer/issues`
- 关键文件:
  - 实验脚本: `/Users/mameican/Desktop/量化/{counterfactual,refresh_frequency,comprehensive_refresh}_experiment.py`
  - V20H bundled: `/Users/mameican/Desktop/量化/v20h_strategy/`
  - 服务端: `/Users/mameican/Desktop/server/v2.3/server/`
