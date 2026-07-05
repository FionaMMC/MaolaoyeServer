# Spec: 多租户子账户台账 + v7.9 小市值策略集成（thin relay）

**日期**: 2026-07-05
**状态**: 设计完成，待 review，再走 writing-plans 出实施计划
**作者**: Claude (brainstorming with Meican)
**前置背景**:
- 策略来源：`/Users/mameican/Desktop/small_cap_peer/round4/v7.9/baseline.py`
- 审计：`small_cap_peer/audit/v79审核_幸存者审计bug_meican.md`（2026-07-05, Meican 复核）
- 集成方法模板：[2026-05-24-v53-allweather-bottom-integration-design.md](2026-05-24-v53-allweather-bottom-integration-design.md)

---

## 0. 目标与范围

把同事 `small_cap_peer` 的 **v7.9 小市值轮动策略**推上 QMT 模拟盘，作为现役 `paper_v20h` + `paper_v53` 之外的第 3 个策略，运行在**同一个** QMT 模拟账户 `301300148788` 上（**没有第二个 QMT 账户**）。

关键前提：v7.9 与 v20h **都是个股权益策略**，理论上可能持有同一只票。现有的"按 symbol 归属"白名单对账模型只在各策略 universe 互不相交时成立（v53 是 ETF、v20h 是 CSI1000 个股，天然不相交），**无法扩展到多个可能重叠的个股策略**。且用户明确"之后还会上更多策略"，因此本 spec 不做一次性打补丁，而是把对账层演进为**多租户子账户台账模型**，v7.9 作为该模型的第 3 个租户上线。

**本 spec 覆盖**：
- **Part A（Foundation）**：对账层从"按 symbol 白名单"演进为"台账总量对账"；退掉 v20h legacy claim-all 模式；预算纪律。这是可扩展地承载 N 个策略（含重叠个股策略）的地基。
- **Part B（Tenant）**：v7.9 以 **thin signal relay** 方式集成——策略在本地（缓存所在处）算出每周目标篮子，server 只存储 + diff + 下单，不在 server 跑选股。
- M0 dry-run + M1 paper 上线的分阶段门槛。

**本 spec 不覆盖**：
- M2 实盘真钱
- 在 server 端重跑 v7.9 选股（full port）——明确否决，走 thin relay
- v7.9 策略算法本身的优化/重新回测（用同事既有 frozen 信号）
- 修 v7.9 研究侧的审计 open items（`common.py:137` 等，见 §4）——属同事 repo，非本 server 集成范围；但对 go-live 的影响在 §9 列明

---

## 1. 关键决策（brainstorming 输出）

| # | 决策点 | 选定 | 理由 |
|---|---|---|---|
| 1 | 计算位置 | **Thin relay**（本地算篮子，server 存+执行） | v7.9 需全市场 PIT 基本面+退市数据选最小 50，server 只有 ETF 窗口数据；relay 最轻，契合"冻结信号做前向验证"的定位 |
| 2 | QMT 账户 | **共用 `301300148788`**（无第二账户） | 用户确认没有第二个 QMT 模拟账户 |
| 3 | 对账模型 | **台账总量对账**（弃 symbol 白名单） | v7.9 与 v20h 都是个股、可能重叠，白名单无法表达"一只票被两个台账共同持有"；且要为后续更多策略扩展 |
| 4 | 归属方式 | **下单血缘**（order_id → instance，via `OrderSignalMap`） | 归属在下单时定死，不做事后"认领"；成交按 order_id 精确回到下单的那个子账户 |
| 5 | v20h legacy 模式 | **退掉**，改成普通台账实例 | legacy "claim everything else" 会吞掉别的策略持仓，是多策略下最大的雷 |
| 6 | 资金纪律 | 每个台账固定虚拟预算，`Σ 预算 ≤ 真实可用资金` | 不引入跨策略拆 cash 的循环依赖 |
| 7 | v7.9 启动预算 | **¥10,000,000**（与 v20h/v53 同量级），**待真实账户可用资金确认** | 若真实账户不够 3×¥10M，v7.9 降额（见 §9 O2）；spec 按 cash-agnostic 写 |
| 8 | 上线节奏 | 先在 v20h+v53 上验证新对账（应与旧结果一致），v7.9 全程 `dry_run` 到对账证明可靠；退 v20h legacy 单独设验证门 | 对账/结算层有历史伤疤（孤儿 SELL / order_id 重生成 / `_clear_for_date`），必须分阶段 |

---

## 2. 背景：为什么白名单模型不可扩展

现有 `strategies.yaml` 用 `owned_symbols` 把共享 QMT 账户按 symbol 切给各实例；`reconcile.py` 据此过滤 QMT 持仓，`validate_no_overlap()` 在启动时禁止任何 symbol 被两个实例同时拥有。

这套只在**各策略 universe 互不相交**时成立：
- v53 = 10 只 ETF（静态）；v20h = CSI1000 个股。ETF code 永远不会是 v20h 选的股票 → 零重叠、且 v53 白名单永不变 → 成立。

一旦有**两个可能重叠的个股策略**（v20h + v7.9，都是小盘股），白名单模型有三处结构性失败：
1. **无法表达共同持有**：若 X 同时被 v20h、v7.9 持有，QMT 只有一个聚合持仓，白名单要求 X 属于唯一 owner → `validate_no_overlap` 直接抛错，或被迫二选一 → 归属错误。
2. **动态 universe**：v7.9 每月轮换 50 只，白名单要每次调仓重写，且启动期 `validate_no_overlap` 看不到盘中轮换。
3. **v20h legacy claim-all 是活雷**：v20h `owned_symbols: null` 会认领账户里所有未被别人白名单声明的持仓；v7.9 白名单一旦过期或有一只重叠，v20h 的 reconcile 会**静默吞掉 v7.9 的个股持仓**进自己的台账，两本账 desync。

> v7.9 的 pool 是**全市场市值最小 ~50**（`common.py:69` `load_pool`；`000852.SH` 只用于交易日历，不是选股 universe），排名约 4950–5000；v20h 是 CSI1000（排名约 801–1800）。**不同市值层，实践中几乎不相交**——但"几乎"正是问题：重叠不再是结构性为零，而是每月边界的经验偶然。为可扩展、也为诚实，本 spec 不赌这个"几乎"，直接换模型。

---

## 3. Part A — 多租户子账户台账（Foundation）

### 3.1 核心不变式

一个真实 QMT 账户 = N 个服务器端虚拟台账之和。对账只验证这个"和"，不再问"这股是谁的"。

```
对每个 symbol X:   QMT_position[X]  ==  Σ_i  instance_i.virtual_positions[X]
现金:              QMT_cash         ==  Σ_i  instance_i.virtual_cash          (容差内)
```

- 台账各记各的（`instance_state.virtual_cash` / `virtual_positions`，**现已存在，不新建**）。
- 归属由**下单血缘**决定：每笔聚合单归唯一 instance（`OrderSignalMap: order_id → signal_id → instance_id`）；成交按 order_id 回到下单的那个 instance 更新其台账。**没有"认领"步骤。**
- QMT 真实账户退化成"总账"，唯一用途：核对上式。对得上=三本台账都没记错；对不上=报警。

### 3.2 三处改造

**改造 1 — 总量对账（取代 symbol 白名单过滤）**

`ReconcileService`：
- **持仓**：对每个 symbol，断言 `Σ_i virtual_positions[X] == QMT_position[X]`。相等 → 全部台账可信；不等 → 报警（微信通道），**不自动改台账**（避免像旧 reconcile 那样强对齐引入 desync）。
- **现金**：泛化 v53 spec 已定义的 `reconcile_cash_total()`——`Σ virtual_cash ≈ QMT_cash`，超容差报警。
- 删除/停用 per-instance `owned_symbols` 白名单过滤路径与 `validate_no_overlap()`（或降级为纯诊断，不再作为归属依据）。
- **容差**：现金 `tolerance` 沿用 v53 的 5%；持仓要求整数精确相等（成交都是整股，台账应逐股对齐；出现非零差即 bug，报警）。

**改造 2 — 退掉 v20h legacy claim-all**

- v20h 实例改为普通台账：`virtual_positions` 就是它的账，不再"认领账户里其他一切"。
- 迁移：上线新对账前，把 v20h 当前真实持有的个股快照写入其 `virtual_positions`（一次性 seed），之后靠成交血缘自然演进。
- 这是**风险最高的单步**，单独设验证门（§8）。

**改造 3 — 预算纪律**

- 每个 instance 保留固定 `virtual_initial_cash`（v20h/v53/v79 各 ¥10M，v79 待 §9 O2 确认）。
- 全局规则：`Σ(virtual_initial_cash) ≤ 真实账户可用资金`。真实账户须注资到三者之和；`reconcile_cash_total` 做 tripwire。
- precheck 维持现状（每 instance 对自己 `virtual_cash` 校验），**不引入跨策略共享 cash 的盘中门**——只要预算之和 ≤ 真实资金，就不会透支。

### 3.3 为什么不需要"净额撮合/拆单"

聚合现按 `(account_group, symbol, direction)` 归并——**同一 symbol 的两个 instance 的单不会合并**，各自作为独立聚合单下到同一 QMT 账户。这没问题：
- 同向（都 BUY X）：两张单都成交，各自 order_id 归各自 instance，归属干净。
- 反向（v20h SELL X、v7.9 BUY X）：模拟盘集合竞价各自成交，各自归属；只是多付一次价差/费，**不是正确性问题**。

故**不引入 netting + 按比例拆成交**这套复杂度。每张聚合单归唯一 instance，成交映射天然干净。

### 3.4 可扩展性收益

加第 4、5 个策略 = 加一个 plugin + 一行 instance config。无论它与既有策略 universe 是否重叠，总量对账都成立，无需任何 per-pair 工程。这是本 spec 的核心地基。

---

## 4. Part B 前置 — v7.9 策略画像与审计状态（诚实标注）

### 4.1 策略画像

`round4/v7.9/baseline.py`：
- **核心持仓**：全市场市值最小 ~50 只 A 股（TOP50，ROE>0 且同比改善 + ST/停牌过滤），**月度轮换**。
- **周度 overlay**：T1/Aux 风控 gate，风险周把部分/全部仓位切到防御性 "Hydra" sleeve（v48 全天候的债券腿）。
- **回测**：31.14% 年化 / 1.41 Sharpe / -22.72% maxDD（gross）；30.51% / 1.38（net，含成本）。

对 relay 的含义：server 每周收到的是**已混合的最终目标**——`{50 只股票 × 各自权重 × 股票仓位比例}` + `{防御 ETF × 防御比例}`，拍平成一个 `{code: weight}`（≈1.0 减 cash buffer）。relay 不需要理解内部 gate 逻辑，只执行 diff。

### 4.2 审计状态（重要）

`AUDIT_STATUS.md` 与 Meican 2026-07-05 复核结论：状态是 **PENDING FORWARD VALIDATION，非"审计通过"**。
- 策略本身可信（幸存者收益偏差实测很小，ROE+最小50 双过滤有效）。
- **但** phase20 幸存者认证被一个日期 bug（`common.py:137` 退市股价格全解析成 NaN）废掉；ML 修复层是 2026-06 样本内动机、**未经 OOS 证明**。
- 处方正是：**冻结信号，把 post-2026-07 当作真正的前向验证样本**——**所以推模拟盘本身就是审计规定的下一步**。定位为"冻结信号 + 前向验证"，**不是**"审计干净、可上真钱"。
- 4 项 open items（`common.py:137` 修复、重跑 phase20、补 84 只空退市数据、Hydra 换源验证）属研究侧，见 §9。

---

## 5. Part B — v7.9 Thin Relay 集成（Tenant）

### 5.1 总体数据流

```
Mac 本地（small_cap_peer repo，缓存所在）
  每周（调仓周更）跑 forward-step 脚本:
    冻结的 v7.9 引擎 → 本周最终目标篮子 {code: weight}（股票 sleeve + 防御 ETF）
    → 生成 v79_target_YYYYMMDD.parquet
    → POST /admin/upload-data (strategy_name=v79)
        │
        ▼
Server APScheduler cron (Asia/Shanghai 16:00)
  StrategyPipeline.run_all(T+1)
    ├─ V20HAdapter.run(...)     （已有）
    ├─ V53Adapter.run(...)      （已有）
    └─ V79RelayAdapter.run(ctx, T+1)
          ├─ 本周是否有新目标篮子（决策日判断）? 否 → return []
          ├─ 读最近上传的 v79_target parquet
          ├─ NAV = ctx.cash() + Σ(持仓 × ref_price)
          ├─ 权重 → 数量（100 股整；SELL offset 0 / BUY offset +0.005）
          ├─ 风控过滤（流动性 / ST-退市 / 涨跌停 / blacklist）
          ├─ diff vs ctx.positions()（v79 自己的台账）→ SELL 先 BUY 后
          └─ return RawSignal[]（instance=paper_v79_v79_relay）
        ▼
  raw_signals → precheck → aggregate(account_group,symbol,dir) → orders(PENDING, account_group=paper_v79)
        ▼
  T 日 18:00 人工眼检 GET /orders?date=T+1&account_group=paper_v79
  T+1 09:10 集合竞价前 client 自动下单
        ▼
  成交 POST /trade-result → SettlementService 按 order_id 血缘更新 paper_v79 台账
        ▼
  每日 reconcile：Σ 三台账 == QMT（§3.1）
```

### 5.2 目标篮子契约（上传文件 schema）

`v79_target_YYYYMMDD.parquet`：

| 列 | 类型 | 说明 |
|---|---|---|
| `code` | str | QMT 格式（`000638.SZ` / `511260.SH`），股票+防御 ETF 混在一张表 |
| `weight` | float | 目标权重，Σweight ≈ 1 − cash_buffer |
| `sleeve` | str | `equity` / `defensive`（诊断/展示用，relay 执行不依赖） |
| `decision_date` | str | 该篮子的决策日 YYYYMMDD（幂等/防重放） |

- v7.9 用的 code 已是 QMT 格式（peer repo 里就是 `000638.SZ` 等），无需映射表。
- relay **只信最近一个 `decision_date` 未被消费过的篮子**；同一 decision_date 重复上传幂等（防 order_id 重生成类事故，见 memory `orderid_regen_unmatched`）。

### 5.3 V79RelayAdapter 接口

```python
class V79RelayAdapter(Strategy):
    name = "v79_relay"
    data_dir = _V79_DIR / "data"
    data_files = ["v79_target_latest.parquet"]   # 上传覆盖同名；或按日期存+读 max

    def run(self, ctx, trade_date):
        target = parse_yyyymmdd(trade_date)
        basket = self._load_latest_basket()               # 读上传的目标篮子
        if basket is None or self._already_consumed(ctx, basket.decision_date):
            return []                                      # 无新篮子 / 已消费
        nav = self._compute_nav(ctx, target)              # cash + Σ(qty×ref_price)
        target_qty = self._weights_to_quantities(basket, nav, ctx, target)  # 100 股整
        target_qty = self._apply_risk_filters(ctx, target_qty, target)      # 流动性/ST/涨跌停/blacklist
        signals = self._diff_and_emit(ctx, ctx.positions(), target_qty)     # SELL 先 BUY 后
        self._mark_consumed(ctx, basket.decision_date)    # 幂等标记（写 strategy_state）
        if self._cfg.dry_run:
            logger.info("V79 DRY-RUN target_qty=%s signals=%d", target_qty, len(signals))
            return []
        return signals
```

复用 v53 已有的成熟件：`_compute_nav` / `_weights_to_quantities`（100 股整、`nav×(1−cash_buffer)×w/price`）/ `_resolve_reference_price`（ctx.market 最近 close，回退 bundle）/ `_diff_and_emit`（SELL offset 0、BUY offset +0.005）。

### 5.4 小市值执行现实（风控钩子）

微盘/壳股比 ETF 脆弱得多，`config.yaml` 风控需专门处理：
- **流动性**：`当日成交量 < liquidity_multiplier × 目标买入量` → skip 或截断。微盘 ¥200k 单可能占日成交显著比例。
- **ST / 退市**：接 `ctx.risk_blacklist()`；持有期内标的转 ST/退市要能识别并（视规则）清仓。冻结信号选股本身已 ROE+最小50 过滤，但持有中退市是活风险（审计 §一句话结论：选入退市股终局 −80%）。
- **涨跌停不可成交**：微盘一字板常见，client 侧下单要能识别（v20h 已有类似处理路径可参考）。
- **主板微盘**：v7.9 的最小 50 基本是主板/中小板 6 位码，非科创 688——v20h 的 KCB 200 股地板逻辑此处大概率用不到，确认后可略。

### 5.5 strategies.yaml（新模型下）

```yaml
account_groups:
  - group_id: paper_v20h
    qmt_account_id: "301300148788"
    strategies:
      - strategy_id: v20h_v1_3
        virtual_initial_cash: 10000000
        # owned_symbols 移除 → 普通台账（不再 legacy claim-all，见 §3.2 改造2）

  - group_id: paper_v53
    qmt_account_id: "301300148788"
    strategies:
      - strategy_id: v53
        virtual_initial_cash: 10000000
        # owned_symbols 移除 → 总量对账不再需要；保留仅作诊断可选

  - group_id: paper_v79
    qmt_account_id: "301300148788"
    strategies:
      - strategy_id: v79_relay
        virtual_initial_cash: 10000000   # 待 §9 O2 确认真实资金
        dry_run: true                    # M0；M1 切 false（也可放 plugins/v79/config.yaml）
```

---

## 6. 分阶段上线

**M-1 — 对账地基（不碰 v7.9，先在 v20h+v53 上证明）**

- [ ] 实现总量对账（§3.2 改造1）：`Σ 台账 == QMT`（持仓精确 / 现金容差）。
- [ ] 影子运行：新对账与旧白名单对账**并行跑一段**，对同样的 QMT snapshot 应给出一致结论（v20h+v53 universe 不相交，新旧必须等价）。不一致=新对账有 bug，先修。

**M0a — 退 v20h legacy（风险最高单步，独立门）**

- [ ] 一次性把 v20h 真实持仓 seed 进其 `virtual_positions`。
- [ ] 关掉 legacy claim-all 路径。
- [ ] 连续数日 reconcile：v20h 台账逐股 == QMT 中属于 v20h 的部分（此时 v79 还没上，QMT = v20h + v53，可精确验证）。
- [ ] 通过后才进 M0b。

**M0b — v79 relay dry-run（1 个调仓 cycle）**

- [ ] `plugins/v79/` + `v79_relay.py` 实现；`strategies.yaml` 加 paper_v79（`dry_run: true`）。
- [ ] Mac 本地 forward-step 脚本 + 上传走通；relay 读到篮子、算出 target_qty + diff，log 出信号但 orders 表**无新条目**。
- [ ] 单测 + e2e（§10）。
- [ ] 跑 1 个周更 cycle，确认 relay 算出的目标 ≈ 本地 forward-step 篮子（逐股权重差 < 容差）。

**M1 — v79 paper 实盘（无限期前向验证）**

- [ ] `dry_run: false`。
- [ ] 第一次建仓日 18:00 人工眼检 `/orders?date=T+1&account_group=paper_v79`（~50 笔 BUY + 可能的防御 ETF）。
- [ ] 确认真实账户可用资金 ≥ 三预算之和（§9 O2）。
- [ ] 每日 reconcile 总量；每周更后次日人工查 v79 NAV。
- [ ] 报告一律标注"冻结信号 + 前向验证，ML 增益未经 OOS 证明"（§4.2）。

**M2 — 实盘真钱** ── 本 spec 不覆盖。

---

## 7. 测试策略

### 单元测试 `plugins/v79/tests/`
- `test_load_basket.py`：目标篮子解析 / 缺列 / 空 / 幂等（同 decision_date 重复上传）。
- `test_weights_to_quantities.py`：100 股整、cash_buffer、微盘高价/低价边界。
- `test_diff_and_emit.py`：SELL 先 BUY 后、offset（SELL 0 / BUY +0.005）、清仓路径。
- `test_risk_filters.py`：流动性 / ST-退市 / 涨跌停 单独触发。
- `test_already_consumed.py`：同一篮子不重复下单（防 order_id 重生成事故）。

### 对账测试 `app/services/tests/`（Foundation，最关键）
- `test_total_reconcile_positions.py`：构造含 v20h 股 + v53 ETF + v79 股（含一只人为与 v20h 重叠）的 QMT snapshot，验证 `Σ 台账 == QMT` 成立、重叠票不再抛错、归属由血缘决定。
- `test_total_reconcile_mismatch_alerts.py`：人为制造总量不符 → 报警且不自动改账。
- `test_legacy_retire_equivalence.py`：退 legacy 前后，v20h 对同一 snapshot 的台账结论一致。

### 集成测试 `v2.3/server/tests/integration/`
- `test_v79_pipeline_e2e.py`：非决策日 return []；决策日 dry_run 下 log 目标、orders 表无条目；`dry_run: false` 下产出正确数量 RawSignal 且 account_group=paper_v79。
- `test_three_instance_settlement.py`：三 instance 各下单 → 成交回报按 order_id 血缘分别入账，无串账。

### 对照测试（与本地 forward-step 对比）
- 用 Mac forward-step 某周输出的篮子作 fixture，server relay 拼相同 NAV → 算 target_qty → 与本地预期逐股比对。

---

## 8. 风险与失败模式

| 风险 | 触发条件 | Mitigation |
|---|---|---|
| 退 v20h legacy 引入 desync | seed 快照错 / 关 legacy 时序错 | M0a 独立门 + 连续数日逐股对账 + 影子并行 |
| 总量对得上但内部归属错 | 台账被结算路径外的 bug 改写 | 每单归唯一 instance（血缘），成交只经结算路径改账；对账报警兜底 |
| v79 篮子上传后 server 重跑换 order_id → unmatched | client 拉走信号后 relay 又被触发 | decision_date 幂等 + `_already_consumed`（见 memory `orderid_regen_unmatched`）|
| 真实账户资金不够 3×¥10M 建仓 | M1 首次 v79 建仓 | 上线前确认可用资金 ≥ Σ预算；不够则 v79 降额（O2）|
| 微盘流动性/一字板买不进 | 目标含停牌/涨停微盘 | 流动性 + 涨跌停风控；买不进的当周顺延，relay 下周 diff 自然补 |
| 持有期个股退市 | 冻结信号选入后基本面恶化 | ST/退市 risk_blacklist 识别 + 报警；接受前向验证期暴露（审计已量化偏差小）|
| 防御 sleeve 无可执行标的 | Hydra gate 首次触发 | **O1 上线前必须解**：把 v48 债券腿映射到真实 ETF（见 §9）|
| SELL 加 offset 被 QMT 废单 | 微盘 SELL | 沿用 SELL offset 0（memory `qmt_sell_offset_zero`）|

---

## 9. 开放问题（上线前澄清）

**O1 · 防御 sleeve → 可执行 ETF 映射（阻塞 M1 的 Hydra gate）**
回测里防御腿是 v48 `output_forward_rate_bond/daily_returns.parquet`——一个**合成收益序列**，不是单只可买 ETF。实盘执行必须映射到真实 ETF（如复用 v53 的 10Y 国债 `511260.SH`，或 Hydra 实际成分）。且 Meican 审计 §四已指出这次"换源"未单独验证。
→ **行动**：M0b 前定义防御篮子的真实 ETF 构成；本地 forward-step 输出的 `defensive` 行直接给出可买 code+weight，relay 无脑执行。Hydra gate 首次触发前必须闭环。

**O2 · 真实账户可用资金（阻塞 M1 建仓额）**
`301300148788` 当前买力未知。三预算之和 = ¥30M。
→ **行动**：go-live 前拉账户可用资金；≥¥30M 则 v79 ¥10M；否则按剩余买力给 v79 定额（spec 按 cash-agnostic 写）。

**O3 · v7.9 研究侧 open items 对 go-live 的影响**
`common.py:137` 日期 bug 使退市股在**审计**里隐形——但对**实时选股**影响是：当前 listed 股不受影响，故 forward-step 选出的当周篮子可信；bug 主要污染的是历史幸存者认证，不是前向信号。
→ **行动**：M1 前建议同事修 `common.py:137` 并重跑 phase20（不阻塞 relay 集成，但阻塞"审计通过"的正式命名）；relay 用冻结信号照常前向验证。

**O4 · Mac forward-step 周更链路**
谁、何时、跑什么脚本产出每周篮子并上传？（类比 v20h pred 周更是纯手动链路，停更→静默冻结，见 memory `v20h_pred_refresh_ops`——同样的运维脆弱性。）
→ **行动**：写 `refresh_v79_basket.sh`（Mac 本地，`small_cap_peer/`）；handbook 写明周更 SOP + 停更即冻结的告警。

**O5 · 决策日/周更节奏对齐**
v7.9 是周度 overlay + 月度选股。relay 的"决策日判断"以本地上传的 `decision_date` 为准（server 不自己算周末/月末），最简。
→ 确认周更提交日（周几）与 QMT 下单日（T+1 竞价）对齐。

---

## 10. 实施顺序（writing-plans 阶段细化）

粗粒度 step：
1. **Foundation**：总量对账 `ReconcileService`（持仓精确 + 现金容差泛化 `reconcile_cash_total`）+ 影子并行开关 + 对账测试。
2. **Foundation**：退 v20h legacy（seed 快照 + 关 claim-all）+ 等价性测试（M-1/M0a 门）。
3. `plugins/v79/`：`config.yaml` + `v79_relay.py`（篮子解析、NAV、权重→量、风控、diff、幂等）+ 单测。
4. Mac 本地 `refresh_v79_basket.sh`（forward-step 冻结引擎 → 篮子 → 上传）+ 对照测试。
5. `strategies.yaml` 加 paper_v79（dry_run）；e2e。
6. 防御 sleeve 真实 ETF 映射（O1 闭环）。
7. Dashboard 扩展三 instance 视图（NAV 三线 + v79 目标 vs 实际 diff）。
8. `V79_OPERATIONS_HANDBOOK.md`（仿 V53，加小市值执行/退市/周更 SOP + 冻结信号免责标注）。
9. M0b dry-run 跑 1 个 cycle。
10. O2 资金确认 → M1 切 `dry_run: false`。

---

## 11. Out-of-scope（防 scope creep）

- M2 实盘真钱。
- server 端重跑 v7.9 选股（full port）。
- 修 v7.9 研究侧审计 items（属同事 repo；仅在 §9 标注对 go-live 的影响）。
- netting/按比例拆成交（§3.3 已论证不需要）。
- 第 4、5 个策略的具体集成（本模型已为其留好地基，各自另开 tenant spec）。
- 期货对冲 / 期权。
