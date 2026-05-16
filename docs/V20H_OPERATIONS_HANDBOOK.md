# V20H 运维手册

**版本**: 1.0 (2026-05-17，5-bug 修复后)
**目标读者**: 你（策略主理人）+ 搭档（系统运维）
**适用范围**: paper_v20h_v20h_v1_3 实例（1000 万模拟盘）

---

## 0. 一句话总览

V20H 是 **CSI1000 / 中证 1000 多头 + 期货对冲** 的量化中频策略。

- **信号**: V18 ML 模型每天给 ~900 只股票一个 `prob_top` 评分；剔除底部 10% → 留 ~810-857 只目标仓位
- **调仓**: 严格每 42 个交易日 1 次主调仓；中间只有 cap_weight 微调
- **对冲**: V12 regime 信号下穿阈值时用 IM 期货空头分级对冲（30% / 70% / 100%）
- **历史表现**: 5 年回测 Sharpe 0.91-0.95，年化 +12.5% ~ +18.7%，最大回撤 -17.2%
- **本周实盘**: 5/8 起步，5/18 NAV ¥10,014,568（vs CSI1000 -0.67%，alpha +0.82%）

⚠️ **当前生产 Phase 14c**：股票实盘 + 期货 SKIP。也就是说**对冲层目前是不工作的**，只跑纯多 + cap_weight + vol_target。

---

## 1. 今天的状态（2026-05-17）

| 项 | 值 |
|---|---|
| 实例 | `paper_v20h_v20h_v1_3` |
| 初始资金 | ¥10,000,000 |
| NAV (5/18) | ¥10,014,568 (+0.15%) |
| 持仓数 | 852 只 |
| 现金 | ¥434,181 (4.3% of NAV) |
| 黑名单 | 63 条（49 自动 + 13 ST + 1 手工，其中 43 条是 688/689 科创板，5/18 deploy 后科创板会被前置过滤、不再进黑名单） |
| 5/18 PENDING orders | 4 条（2 SELL + 2 BUY，含 1 个 688498.SH 科创板，将 REJECTED） |
| strategy_state | null（5/18 第一次跑会写入） |

---

## 2. 策略机制详解

### 2.1 选股 + 调仓

```
T 日收盘后 → V18 ML 推理（本地 daily_v20h.sh） → pred_csi1000.parquet 推到服务器
                                                ↓
T+1 早上 9:00 → server 跑 V20H adapter →
    1. pred_today = pred_df 中 ≤ T+1 的最近日期那批 (~1000 行)
    2. 剔除 688/689 科创板（账户没权限）→ ~957
    3. 剔除黑名单（自动 + 手工）→ ~857
    4. 按 prob_top 排序，剔除底部 10% → ~771 个 target_codes
    5. 检查是否到 rebal 日（di - last_rb_idx >= 42）:
        - 是 → 卖掉非 target、买入 target 里没持有的
        - 否 → 只做 cap_weight 微调（单股超过 1.5×均权 → 卖至均权）
    6. emit RawSignal[]（SELL 先 BUY 后）
                                                ↓
9:00 - 9:25 → client 拉 /orders?date=T+1 → QMT 集合竞价下单
                                                ↓
9:30 开盘 → QMT 撮合
                                                ↓
15:00 收盘 → client 拉 trades_today → POST /trade-result
                                                ↓
15:01 - 15:10 → server settlement: 扣手续费/印花税、更新 instance_state、写 perf_snapshot
                                                ↓
15:11 → client 推 EOD OHLCV (/admin/upload-data) 给服务器（V20H 用不上，但 server 留着给其他策略）
```

### 2.2 风控三层（实盘**只生效前两层**）

| 层 | 何时触发 | 实盘状态 |
|---|---|---|
| 1. cap_weight | 每天，单股价值 > 1.5×（总价值/持仓数）→ 卖出超出部分 | ✅ 生效 |
| 2. vol_target | 每天，realized_vol_ann > 15% target → 缩仓位至 max(0.3, 15%/realized) | ✅ 生效 |
| 3. V12 graduated_4 期货空头 | 每天，V12 < Q40 → 30% short；< Q20 → 70%；< Q10 → 100% | ❌ **Phase 14c skip**，期货 v2.4 才接 |

⚠️ **没有期货对冲，意味着 2022 那种熊市 V20H 会跟着 CSI1000 一起跌**。在 v2.4 接入期货之前，组合实质是 long-only CSI1000 with smart selection + vol scaling。

### 2.3 数据依赖

| 文件 | 路径（服务器） | 谁负责更新 | 频率 |
|---|---|---|---|
| pred_csi1000.parquet | `/opt/qmt-server/v2.3/server/plugins/v20h/data/` | 你（本地 daily_v20h.sh） | 每周一次最低（研究文档：1-42 天等价） |
| v12_exp_hs300.parquet | 同上 | 你（同脚本） | 同上 |
| index_csi1000.parquet | 同上 | 你（同脚本） | 同上 |
| stock_close.parquet | 同上 | 你（同脚本） | 同上 |
| stock_returns.parquet | 同上 | 你（同脚本） | 同上 |

上传方式：
```bash
# 本地（Mac）跑完 daily_v20h.sh 之后:
curl -X POST -H "Authorization: Bearer $QMT_API_KEY" \
  -F "strategy_name=v20h_v1_3" \
  -F "files=@/Users/mameican/Desktop/量化/walkforward_framework/cache/v18/pred_csi1000.parquet" \
  -F "files=@..." \
  http://120.26.138.82:8000/admin/upload-data
```

(或用 `v20h_refresh.py` 里 sync 部分自动 push)

---

## 3. 预期收益（5 年回测，研究文档 2026-05-16）

| 指标 | 数值 |
|---|---|
| 年化 alpha | +12.5% ~ +18.7% (vs CSI1000) |
| Sharpe | 0.91 - 0.95 |
| Max DD | -17.22% |
| 最差年（2022 大熊） | -3.12% (with hedge) / 没 hedge 时会更深 |
| 最好年（2021 大牛） | +48.62% |
| 横盘年（2023） | +1.73% |
| Bootstrap 1000 次 CI | Sharpe diff CI [-0.20, 1.61]（refresh 1-42 天**统计上等价**） |

**重要 caveats**:
- 上面是**含期货对冲**的回测；当前 Phase 14c 没期货，下行风险更大
- 2022 -3.12% 那年 graduated_4 hedge 提供了 ~5% 防御。没 hedge 时回测显示 -8% ~ -10%
- 5 年样本下 Sharpe SE ≈ 0.10-0.15，**任何 < 0.10 的 Sharpe 差异都不显著**
- 单周/单月数据**绝不能下结论**（本周 +0.15% 是噪音，不是 alpha 兑现）

---

## 4. 系统架构

```
┌─────────────────────┐         ┌─────────────────────────┐
│  Mac (你)            │         │ Aliyun ECS              │
│                     │  Push   │ 120.26.138.82:8000      │
│  ML pipeline:       │ ──────▶ │                         │
│  daily_v20h.sh      │ pred    │ /admin/upload-data      │
│  → V18 retrain      │         │                         │
│  → pred_csi1000     │         │ FastAPI:                │
│                     │         │  /admin/run-pipeline    │
│  ./venv/bin/python  │         │  /orders, /trade-result │
│                     │         │  /admin/health          │
└─────────────────────┘         │  /admin/strategy-state  │
                                │  /admin/bookkeeping-... │
                                │  /dashboard             │
┌─────────────────────┐         │                         │
│ Windows (搭档)       │         │ SQLite:                 │
│                     │  HTTP   │  instance_state         │
│  QMT (xtquant):     │ ──────▶ │  orders / raw_signals   │
│  trigger_pipeline   │         │  trades / perf_snapshot │
│  data_collector     │         │  risk_blacklist         │
│  order_submit       │         │                         │
│  trade_result_push  │         │ systemd:                │
└─────────────────────┘         │  qmt-server.service     │
                                └─────────────────────────┘
```

### 4.1 谁干什么

| 组件 | 谁 | 干什么 |
|---|---|---|
| Mac 本地 ML | 你 | V18 retrain + pred 生成 + push 到云 |
| ECS 服务器 | server 自动 | 策略执行、信号聚合、订单队列、对账、NAV 快照 |
| Windows QMT 客户端 | 搭档 + cron | 触发 pipeline、拉单、下单、回报推送 |

---

## 5. 5/18 周一交接清单

> ⚠️ 修复刚 deploy（5/17 22:19），当前 5/18 仍有 4 条 PENDING orders 是修复前生成的（含 1 个会被 REJECTED 的科创板）。**必须先清掉重新生成**才能稳。

### 5.1 周一早上 8:30 — 周一开盘前

```bash
# 1. 服务器健康度自查
curl -sH "Authorization: Bearer $QMT_API_KEY" \
  http://120.26.138.82:8000/admin/health | jq

# 关键字段：
#   pred_status.lag_hours  → 应该 < 168（7 天内）
#   blacklist.merged_total → 应该 ~63
#   instances[0].virtual_cash → ~434K
#   instances[0].holdings_count → 852

# 2. 看 5/18 还挂的 PENDING（修复前生成的旧单）
curl -sH "Authorization: Bearer $QMT_API_KEY" \
  "http://120.26.138.82:8000/admin/orders?date=20260518&status=PENDING" | jq

# 应该看到 4 条：
#   001309.SZ SELL 300, 601778.SH SELL 31900,
#   688498.SH BUY 200, 301171.SZ BUY 11400
```

### 5.2 周一 9:00 — 触发 pipeline 重生成 5/18 信号

```bash
# 用新代码重新跑 → 幂等机制会清掉旧 4 条 + 按新逻辑（含科创板过滤 + SELL 优先）重写
curl -X POST -H "Authorization: Bearer $QMT_API_KEY" \
  "http://120.26.138.82:8000/admin/run-pipeline?trade_date=20260518" | jq

# 期望输出：
#   "signals": <数量>, "passed": <略少>, "orders": <几条>
#   关键：科创板 688498.SH 不应该出现在新 orders 里
```

或者用客户端脚本：
```powershell
# Windows 端
cd C:\parttime\MaolaoyeServer
git pull origin master   # ← 拉最新代码（含 5-bug fix）
.\venv\Scripts\python.exe v2.3\client\trigger_pipeline.py --date 20260518
```

### 5.3 周一 9:10 — 验证新 orders

```bash
curl -sH "Authorization: Bearer $QMT_API_KEY" \
  "http://120.26.138.82:8000/admin/orders?date=20260518&status=PENDING" | jq '.data.items | length'

# 验证：
# - 没有 688/689 前缀的 symbol
# - SELL 应该排在最前面（不强制但客户端会按 SELL 先排）
```

### 5.4 周一 9:15-9:25 — 集合竞价：客户端下单

搭档侧（Windows）：
```powershell
.\venv\Scripts\python.exe v2.3\client\order_submit.py
```

跑通后会出现 WeChat 通知 / 日志条目，记录每笔 PASS / FAIL。预期：
- 大部分 SELL/BUY PASS → 提交到 QMT
- 个别可能 FAIL（QMT 内部风控）→ 进黑名单

### 5.5 周一 15:00-15:30 — 收盘后回报推送 + NAV 快照

```powershell
.\venv\Scripts\python.exe v2.3\client\trade_result_push.py
```

之后服务器自动：
- settlement 更新 instance_state（含手续费 + 印花税）
- 写 perf_snapshot
- **如果有"防穿仓静默拒绝"会触发 `bookkeeping_divergence` flag**

### 5.6 周一晚上 — 验证当日

```bash
# 当天 NAV 是否合理
curl -sH "Authorization: Bearer $QMT_API_KEY" \
  "http://120.26.138.82:8000/admin/nav-history?instance_id=paper_v20h_v20h_v1_3&limit=5" | jq

# 对账分叉（必须 = 0）
curl -sH "Authorization: Bearer $QMT_API_KEY" \
  "http://120.26.138.82:8000/admin/bookkeeping-divergence" | jq '.data.count'

# 策略状态（应该看到新写入的 last_rb_idx / equity_history 等）
curl -sH "Authorization: Bearer $QMT_API_KEY" \
  "http://120.26.138.82:8000/admin/strategy-state" | jq
```

---

## 6. 日常运维 SOP

### 6.1 每日（每个交易日）

| 时间 | 谁 | 做什么 |
|---|---|---|
| 8:45 | 自动 / 搭档 | 周一-周五 8:45 cron 跑 `trigger_pipeline.py` 算当天信号 |
| 9:15 | 搭档 | `order_submit.py` 集合竞价下单 |
| 15:30 | 搭档 | `trade_result_push.py` 推回成交 |
| 17:00 | 你 | 看 `/dashboard` 或 `curl /admin/health` 检查当日 |

每日**必看**:
1. `bookkeeping_divergence` 总数 — **必须 = 0**，非 0 当天就要查
2. NAV 是否在合理范围（vs CSI1000）
3. holdings_count 是否稳定（首次 deploy 后会从 852 → ~857 收敛）

### 6.2 每周日晚（pred 刷新）

```bash
cd /Users/mameican/Desktop/量化
bash daily_v20h.sh
# 会自动 V18 walkforward inference + push pred 到服务器
```

> 研究文档结论：refresh 1-42 天**统计上等价**（bootstrap p=0.077）。所以每周一次足够，**不要每天跑**（浪费 ~15 分钟训练时间）。

### 6.3 每季度第一周（V18 模型重训）

```bash
cd /Users/mameican/Desktop/量化
./.venv/bin/python -m walkforward_framework.run_v18_final --rebuild
# 把所有历史数据重新走 expanding IC scoring，输出新版 pred
```

这是**重训模型参数**，不是单纯刷新推理。耗时约 15-20 分钟。

季度日期（自然季）：
- Q1: 1 月第一周
- Q2: 4 月第一周
- Q3: 7 月第一周
- Q4: 10 月第一周

### 6.4 每年（walkforward 复检）

每年 1 月做一次完整 walkforward 实验：
- 用最新一年的 OOS 数据复现回测
- 检查 refresh 频率最优值有没有漂移
- 检查 Sharpe / MaxDD 是否仍在历史区间
- 看 bootstrap CI 是否随着样本增加变窄（如果是，说明 alpha 显著起来）

输出报告位置：`docs/research/v20h_refresh_frequency_study.md`（追加新章节）

---

## 7. 信号机制

### 7.1 信号怎么从 V20H 生成的

每次 pipeline 跑（每天 9:00 左右）：

```python
# 1. 从 instance_state 读取当前 cash + positions
ctx_positions = {qmt_to_v20h_code(qmt): qty for qmt, qty in ctx.positions().items()}

# 2. 从 pred_csi1000.parquet 读取 prob_top
pred_today = pred_df[date <= target_date].nlargest_date()  # 取 ≤ T+1 的最新

# 3. 三层 universe 过滤
pred_today = pred_today[~code.startswith(("688","689"))]    # 板块权限
pred_today = pred_today[~code.isin(blacklist)]             # 历史 REJECTED
pred_today = pred_today[~bottom 10% by prob_top]           # CUT10

# 4. 调用 V20HStrategy.step():
#    - apply_cap_weight 卖掉超权重的
#    - 主调仓（rebal day）：卖非 target，买 target - current（按 cash / N 平均分配）
#    - 不在 rebal day：什么都不做

# 5. diff target_positions vs before_positions → RawSignal[]
to_buy  = {c: q for c, q in target.items() if q > before.get(c, 0)}  # 新增/加仓
to_sell = {c: before[c] - q for c, q in target.items() if c in before and before[c] > q}  # 部分减仓
to_close = {c: before[c] for c in before if c not in target}  # 整只卖光

# 6. 输出顺序：SELL 在 BUY 之前（client / server 都强制 SELL-first）
```

### 7.2 信号种类

| 类型 | 触发原因 | 典型量级 |
|---|---|---|
| **主调仓 BUY** | rebal day + target 里没持有的票 | 每只 ~1/N × NAV |
| **主调仓 SELL** | rebal day + 持有但不在 target | 整只清掉 |
| **cap_weight SELL** | 单股 value > 1.5× 均权 | 卖至均权（小量） |
| **vol_target 缩仓** | realized_vol > 15% → 整体 scale 下调 | 按比例 SELL |

非 rebal 日的 BUY 信号**理论上不应该出现**（除非 cap_weight 卖完后 cash 太多想再买，但代码里 cap_weight 不会触发新 BUY）。**如果非 rebal 日看到 BUY，要查**。

### 7.3 信号 → 订单 → 委托 → 成交

```
RawSignal (server 内存)
   │
   │ precheck（cash 充足 / position 充足 / 100 整数倍）
   │ ⚠ 关键：SELL 先 precheck，把 net 收入累加到 running_cash，再 precheck BUY
   ▼
raw_signals 表（含 precheck_status PASS / FAIL）
   │
   │ aggregate：按 (account_group, symbol, direction) 求和
   ▼
orders 表（status=PENDING）
   │
   │ client GET /orders?date=T+1 → 自己 risk_check → 提交 QMT
   ▼
QMT 撮合（开盘 9:30 → 集合竞价 / 连续交易）
   │
   │ trades_today 拉回 → POST /trade-result
   ▼
trades 表 + orders.status 更新（FILLED / PARTIAL / CANCELLED / REJECTED）
   │
   │ settlement：扣手续费、扣印花、更新 instance_state.virtual_cash + virtual_positions
   ▼
perf_snapshot：每天 EOD 一行（含 NAV + positions_snapshot）
```

---

## 8. 调仓节奏

### 8.1 42 天主调仓（V20H 核心）

```
rebal_freq = 42 个交易日

第 1 个 rebal day → 建仓 (last_rb_idx = di)
                  ↓
第 42 天 → 检查 di - last_rb_idx >= 42 → 触发主调仓 (last_rb_idx 更新到 di)
                  ↓
第 84 天 → ...
```

**为什么 42 天**：
- 研究文档实证：refresh 1-42 天 Sharpe 等价（bootstrap p=0.077 不显著）
- 但 > 42 天衰减明显（63 天 Sharpe 0.91 vs 42 天 0.94）
- 42 天约等于**两个月**——长到避免过度交易摩擦，短到能 ride alpha decay 之前的窗口
- 实证 Korajczyk-Sadka 2004 / AQR Frazzini 2018 都指向月度 - 季度为甜点

### 8.2 非 rebal 日的微调

非 rebal 日：
- cap_weight 会卖超权重的票（典型每天 0-3 笔 SELL）
- 不会有 BUY（除非紧急建仓首次）
- vol_target 不直接生成信号，只在 rebal 日通过 scale 参数影响新买入量

### 8.3 什么时候**强制** rebal

| 情况 | 处理 |
|---|---|
| 首次建仓（新实例） | adapter 自动检测 `len(ctx_positions) == 0` → 立即 rebal |
| 老实例迁移（无 strategy_state） | adapter 自动检测 `last_rb_idx not in persisted` → 立即 rebal 一次（**这正是 5/18 deploy 后第一次跑会发生的事**） |
| 异常累计偏离 | 手工触发：先 reset_instance_states，再 trigger_pipeline |

### 8.4 如何查"下次 rebal 是几号"

```bash
curl -sH "Authorization: Bearer $QMT_API_KEY" \
  "http://120.26.138.82:8000/admin/strategy-state?instance_id=paper_v20h_v20h_v1_3" | jq

# 返回里 strategy_state.last_rb_idx
# 下次 rebal di = last_rb_idx + 42
# pred_df 里第 di 个日期就是下次主调仓日
```

例如 5/18 是首次按新代码跑，假设 di=1247（pred 第 1247 天），那 last_rb_idx 会被设为 1247。下次主调仓 di=1289，对应日历日大约是 **7 月中旬**。

---

## 9. Pred 重训 vs 信号生成 — 两件事

### 9.1 概念分清

| 动作 | 频率 | 干什么 | 谁触发 |
|---|---|---|---|
| **Pred 推理** | 每周 1 次最低 | 用现有 V18 模型，对最新数据出 prob_top | 你（bash daily_v20h.sh） |
| **V18 retrain** | 每季度 1 次 | 重新拟合模型参数 | 你 (`--rebuild` flag) |
| **信号生成** | 每个交易日 | 拿最近 pred 算今天该买/卖 | server 自动（pipeline） |
| **主调仓** | 每 42 个交易日 | V20HStrategy 内部决定要不要换股 | server 自动（strategy.step）|

### 9.2 Pred 多久刷一次足够

研究文档铁证：**1-42 天等价**。所以**每周一次**最实际：
- 时间窗：周日 22:00（不影响下周一交易）
- 命令：`bash daily_v20h.sh`
- 之后自动 sync pred 到服务器

### 9.3 V18 retrain 触发条件

| 触发 | 行动 |
|---|---|
| 定期：每季度第一周 | `./.venv/bin/python -m walkforward_framework.run_v18_final --rebuild` |
| 异常：60 日滚动 Sharpe 跌破 0.5 | 立即 retrain；如果 retrain 后仍 < 0.5 → 暂停策略，做深度诊断 |
| 异常：CSI1000 重大成分调整 | 6 月 / 12 月调整月**结束后**那个周末跑一次 |
| 异常：黑名单 30 天内新增 > 20 个 | 说明大量股票被 REJECTED，可能账户权限/数据问题，需排查 |

⚠️ retrain 后**第一次跑** server 时，pred 完全换了一批 prob_top 排序，可能导致 V20H 在下个 rebal day **大幅换仓**。这是正常的，但要：
- retrain 选在距下个 rebal day 远的日子做
- retrain 当天 push 完 pred 后**别立刻** trigger pipeline，等下个交易日早上自动跑

---

## 10. 监控与告警

### 10.1 每天必看的 5 个指标

```bash
curl -sH "Authorization: Bearer $QMT_API_KEY" http://120.26.138.82:8000/admin/health | jq
```

| 字段 | 健康值 | 异常处理 |
|---|---|---|
| `pred_status.lag_hours` | < 168 (7 天) | > 168 → 立即跑 daily_v20h.sh |
| `instances[0].virtual_cash` | 1-10% of NAV | < 1% → 大概率本周 BUY 偏多，下次 rebal 自动收敛；> 15% → 检查为什么没用上 |
| `instances[0].holdings_count` | 850-900 | < 800 → 黑名单可能爆了，看下面 |
| `blacklist.merged_total` | < 100 | > 100 → 立即调查为什么有这么多 REJECTED |
| `orders_by_date_status_7d` 里 REJECTED 数 | 当天 < 10 | > 10 → 看是不是有新一批 ST/退市 |

```bash
# 必须 = 0
curl -sH "Authorization: Bearer $QMT_API_KEY" \
  "http://120.26.138.82:8000/admin/bookkeeping-divergence" | jq '.data.count'
```

非 0 = QMT 真账户已成交但虚拟账本扣不动 → **当天就要人工对账**（步骤见 11.1）。

### 10.2 Dashboard

浏览器访问 `http://120.26.138.82:8000/dashboard` 看 NAV 曲线 + 当周交易 + 黑名单状态（无需 API key，但只读）。

---

## 11. 应急处置

### 11.1 真账户 ↔ 虚拟账本分叉（最严重）

症状：`/admin/bookkeeping-divergence` 返回非空。

```bash
# 1. 看是哪些 orders
curl -sH "Authorization: Bearer $QMT_API_KEY" \
  "http://120.26.138.82:8000/admin/bookkeeping-divergence" | jq

# 2. 在 QMT 客户端打开账号详情，看真实仓位
#    对比 instance_state.virtual_positions
curl -sH "Authorization: Bearer $QMT_API_KEY" \
  "http://120.26.138.82:8000/admin/strategy-state" | jq

# 3. 手动同步：登录服务器，用 sqlite3 改 instance_state.virtual_cash / virtual_positions
#    让虚拟账本 = QMT 真账户实际状态
ssh qmt
sudo -u qmtserver sqlite3 /opt/qmt-server/v2.3/server/pipeline-server.db
> UPDATE instance_state
>   SET virtual_cash = <真账户现金>,
>       virtual_positions = '{"600519.SH": 100, ...}',
>       last_update = datetime('now', 'localtime')
>   WHERE instance_id = 'paper_v20h_v20h_v1_3';

# 4. 改完后重启服务（safety），让 cache 失效
sudo systemctl restart qmt-server.service
```

### 11.2 服务器宕机

```bash
# 状态
ssh qmt 'systemctl status qmt-server.service'

# 重启
ssh qmt 'sudo systemctl restart qmt-server.service'

# 看错误日志
ssh qmt 'sudo journalctl -u qmt-server.service --since "30 minutes ago" --no-pager'
```

如果数据库锁死（极少见，但 paper trading 早期遇到过）：
```bash
ssh qmt 'sudo systemctl stop qmt-server.service'
ssh qmt 'sudo -u qmtserver sqlite3 /opt/qmt-server/v2.3/server/pipeline-server.db .schema'  # 验证没坏
ssh qmt 'sudo systemctl start qmt-server.service'
```

### 11.3 QMT 客户端断连

搭档 Windows 上：
```powershell
# 重启 QMT 客户端 → 重新登录 → 确认 xttrader 接口
# 然后重新跑当天的 order_submit.py
.\venv\Scripts\python.exe v2.3\client\order_submit.py --date 20260518
```

集合竞价（9:15-9:25）错过的话，可以改成连续交易开盘后下单——但是这会增加滑点。

### 11.4 紧急平仓

如果策略出现严重问题（NAV 单日跌 > 5% 等），需要全部清仓：

```bash
# 选项 A：禁用策略 — server 不再生成信号，但已 PENDING 的还在
ssh qmt 'sudo -u qmtserver sed -i "s/^- group_id: paper_v20h/#- group_id: paper_v20h/" /opt/qmt-server/v2.3/server/strategies.yaml'
ssh qmt 'sudo systemctl restart qmt-server.service'

# 选项 B：手工平仓 — Windows 在 QMT 客户端直接卖
#   1. 暂停 trigger_pipeline / order_submit cron
#   2. 在 QMT 界面选 "全部持仓" → 一键平仓（保留一手做收尾）
#   3. 等 trade_result 推回 server → settlement 把 virtual_positions 清空
```

⚠️ 平仓后**不要立刻重启策略**，先复盘问题根因。研究文档第 9 节是 incident response 模板。

---

## 12. 常用命令速查

### 12.1 服务器侧（SSH）

```bash
# 健康度
ssh qmt 'curl -s http://localhost:8000/healthz'

# 服务状态
ssh qmt 'systemctl status qmt-server.service'

# 日志
ssh qmt 'sudo journalctl -u qmt-server.service --since "1 hour ago" --no-pager | tail -50'

# 跑 migration（部署新 schema 后）
ssh qmt 'cd /opt/qmt-server/v2.3/server && sudo -u qmtserver /opt/qmt-server/venv/bin/python -m scripts.migrate_db'

# 数据库直查
ssh qmt 'sqlite3 /opt/qmt-server/v2.3/server/pipeline-server.db "SELECT instance_id, virtual_cash, json_array_length(virtual_positions) FROM instance_state;"'

# 部署新代码
ssh qmt 'cd /opt/qmt-server && sudo -u qmtserver git pull origin master && sudo systemctl restart qmt-server.service'
```

### 12.2 Admin API（任何能 curl 的地方）

```bash
# 设环境变量
export QMT_BASE=http://120.26.138.82:8000
export QMT_API_KEY=pipeline-v23-shared-secret-2026
auth='-H "Authorization: Bearer '"$QMT_API_KEY"'"'

# 健康度
curl -sH "Authorization: Bearer $QMT_API_KEY" "$QMT_BASE/admin/health" | jq

# 实例状态
curl -sH "Authorization: Bearer $QMT_API_KEY" "$QMT_BASE/admin/strategy-state" | jq

# 当日 orders
curl -sH "Authorization: Bearer $QMT_API_KEY" "$QMT_BASE/admin/orders?date=20260518" | jq

# 订单汇总（最近 7 天矩阵）
curl -sH "Authorization: Bearer $QMT_API_KEY" "$QMT_BASE/admin/orders-summary?days=7" | jq

# NAV 历史
curl -sH "Authorization: Bearer $QMT_API_KEY" "$QMT_BASE/admin/nav-history?instance_id=paper_v20h_v20h_v1_3&limit=30" | jq

# 黑名单
curl -sH "Authorization: Bearer $QMT_API_KEY" "$QMT_BASE/admin/blacklist" | jq

# 对账分叉（必须 = 0）
curl -sH "Authorization: Bearer $QMT_API_KEY" "$QMT_BASE/admin/bookkeeping-divergence" | jq

# 强制刷一个交易日的 pipeline（小心，幂等会清同日旧数据）
curl -XPOST -H "Authorization: Bearer $QMT_API_KEY" "$QMT_BASE/admin/run-pipeline?trade_date=20260518" | jq
```

### 12.3 Mac 本地

```bash
# 推 pred + 一键同步到服务器
cd /Users/mameican/Desktop/量化
bash daily_v20h.sh

# 季度 retrain
./.venv/bin/python -m walkforward_framework.run_v18_final --rebuild

# Dashboard 截图脚本（看 NAV 曲线）
cd /Users/mameican/Desktop/server/v2.3/server
./venv/bin/python scripts/dashboard.py
```

### 12.4 Windows 搭档侧

```powershell
# 进项目目录
cd C:\parttime\MaolaoyeServer

# 拉最新代码
git pull origin master

# 早上 9:00 触发当日 pipeline
.\venv\Scripts\python.exe v2.3\client\trigger_pipeline.py

# 9:15 下单
.\venv\Scripts\python.exe v2.3\client\order_submit.py

# 15:30 回报推送
.\venv\Scripts\python.exe v2.3\client\trade_result_push.py
```

---

## 13. 文档索引

| 文档 | 路径 | 用途 |
|---|---|---|
| **本手册** | `docs/V20H_OPERATIONS_HANDBOOK.md` | 日常运维 |
| 策略研究 | `docs/research/v20h_refresh_frequency_study.md` | 为什么这么设计 |
| 初次部署 | `docs/manual_tests/v20h_paper_trading_runbook.md` | 从零跑通的 step-by-step |
| 项目设计 | `项目设计文档（纯股）v2.1.md` | 整体架构 |
| API 接口 | `API接口文档（纯股）v2.1.md` | server / client 协议 |
| 部署文档 | `v2.3/server/deploy/README.md` | ECS bootstrap |

---

## 14. 变更记录

| 日期 | 改动 | 负责人 |
|---|---|---|
| 2026-05-17 | 初版（5-bug 修复 deploy 后） | 你 + Claude |

---

## 附录 A：5-bug 修复后的行为变化清单

| Before | After |
|---|---|
| 每天主调仓（stateless adapter） | 真 42 天主调仓（持久化 last_rb_idx） |
| 5/13 那种 6 BUY 全 FAIL precheck | SELL 先结算到 running_cash，BUY 再 check |
| settlement 不扣手续费 → 虚拟 cash 高估 | 扣 commission 0.03% + 印花税 SELL 0.05% |
| 防穿仓静默跳过 → 真账户 / 虚拟账本无声分叉 | 升级 ERROR + `bookkeeping_divergence` flag |
| 科创板 688/689 每周都 REJECTED → 进黑名单 | adapter 前置过滤，universe 干净 |
| 残留 real_A_* instance_state 写 0% NAV 快照 | migration 已删 |

## 附录 B：未来 roadmap

| 阶段 | 内容 | 优先级 |
|---|---|---|
| **v2.4** | 接入 IM 期货：自动 short/long、roll、basis 跟踪 | ⭐⭐⭐⭐⭐（**关键，恢复 hedge 才能熊市保命**） |
| **Phase 8** | 实盘 60 天 alpha 累积 → bootstrap CI 是否变窄 | ⭐⭐⭐ |
| **Phase 4** | V18 retrain 频率实验（季度 vs 半年） | ⭐⭐ |
| 多策略并跑 | strategies.yaml 加第二个实例（不同 cut_pct 或 rebal_freq） | ⭐⭐ |
| WeChat / 邮件告警 | bookkeeping_divergence / 服务器宕机自动通知 | ⭐⭐⭐ |
