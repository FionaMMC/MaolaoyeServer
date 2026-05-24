# Spec: v53 全天候 10 资产策略集成 server（作为底舱）

**日期**: 2026-05-24
**状态**: 设计完成，待 review，再走 writing-plans 出实施计划
**作者**: Claude (brainstorming with Meican)
**前置背景**: [策略复现/HANDOFF_to_server_repo.md](../../../../策略复现/HANDOFF_to_server_repo.md)（v53 算法来源与已知 trade-offs）

---

## 0. 目标与范围

把同事 `magicboom1/permenant_portfolio` master 分支的 **v53 全天候 10 资产策略** 集成进当前 v2.3 server，作为长期"底舱"（核心持仓），与现役 `paper_v20h_v20h_v1_3` 并行运行在同一 QMT 模拟账户 `301300148788` 上。

**本 spec 覆盖**：
- M0 dry-run 部署
- M1 paper 实盘
- 支撑这两阶段所需的 server / client / reconcile / ops 改造

**本 spec 不覆盖**：
- M2 实盘真钱切换（待单独评审）
- 期货对冲（v20h 也仍是 Phase 14c skip）
- v53 以外的全天候变种（v48/v54 等）

## 1. 关键决策（brainstorming 输出）

| # | 决策点 | 选定 | 理由 |
|---|---|---|---|
| 1 | 账户隔离 | 同 QMT 账户、不同 `account_group`（`paper_v53` + `paper_v20h`） | 复用现有 QMT 模拟账号，逻辑上分两个 group 便于归因 |
| 2 | 算法版本 | **v53 原计划**（10 资产，dividend 双象限） | 跟同事保持一致，接受 handoff 第 5 节列出的全部 trade-offs |
| 3 | ETF 历史数据 bootstrap | Bundle 历史 parquet + IngestService 增量（类比 v20h） | 可控、零新 ingest 改造 |
| 4 | Vendor 范围 | 只 vendor `weight_methods.py` + `erc_solver.py` + `config.py`，server 自写 adapter / data loader / plumbing | 算法可追溯到同事 master，但 server 不背研究脚本包袱 |
| 5 | Reconcile 改造 | Server 端加 `owned_symbols` 白名单过滤；cash 由 server 自维护 + 总量 sanity check | 不引入 cash 拆分循环依赖 |
| 6 | paper_v53 启动资金 | **¥10,000,000**（与 v20h 同量级） | ETF 持仓粒度最舒服 |

## 2. 总体架构

```
plugins/v53/
├── __init__.py
├── config.yaml                 # 策略参数（含 QUADRANT_MAP、rebal、风控、dry_run）
├── vendor/                     # 同事 master 拷贝（只拷算法核心）
│   ├── __init__.py
│   ├── weight_methods.py       # compute_baseline 双层 inv_vol（含 v48 backward-bias 修复）
│   ├── erc_solver.py           # 备用 ERC 求解器（默认 inv_vol，不调用）
│   └── reference_config.py     # 同事原 config 的 ETF_CODES + QUADRANT_MAP 拷贝（验证用）
├── data/                       # bundled 历史，受 data_files 白名单管控
│   ├── etf_close.parquet       # 10 ETF 历史 close + open
│   └── etf_meta.parquet        # ETF 元数据（list_date / is_qdii / 象限）
├── strategy.py                 # 算法薄壳：拼数据 → 调 vendor.compute_baseline → 输出 {code: weight}
└── tests/

plugins/v53_adapter.py          # Strategy 协议适配；run() 内执行月末判断 + diff + RawSignal[]

app/models/instance_state.py    # ← 改：加 owned_symbols 字段
app/services/reconcile.py       # ← 改：白名单过滤 + cash 自维护
app/scheduler/pipeline.py       # ← 注册 V53Adapter
v2.3/server/strategies.yaml     # ← 加 paper_v53 group
v2.3/server/scripts/migrate_db.py  # ← 加 ALTER TABLE owned_symbols

docs/V53_OPERATIONS_HANDBOOK.md # ← 新写（仿 V20H_OPERATIONS_HANDBOOK.md）

# Mac 本地（不在 server repo）
refresh_v53_bundle.sh           # 月度刷 bundle：sync 同事 master 算法 + 重新生成 etf_close.parquet + curl 推 server
```

**每日 16:00 数据流**

```
APScheduler cron (Asia/Shanghai 16:00)
      │
      ▼
StrategyPipeline.run_all(T+1)
      │
      ├─→ V20HAdapter.run(ctx, T+1)        → 日度信号（已有）
      └─→ V53Adapter.run(ctx, T+1)
            ├─ _is_month_end(T+1)?
            │   ├─ 否 → return []
            │   └─ 是 → 继续
            ├─ 拼 bundle + IngestService 增量 → 252 天 returns matrix
            ├─ vendor.compute_baseline(returns, QUADRANT_MAP, 'inv_vol', ...)
            │   → {code: target_weight}
            ├─ NAV = ctx.cash() + Σ(positions × ref_price)
            ├─ 权重 → quantity（100 股整）
            ├─ 风控过滤（QDII 溢价 / 流动性 / blacklist）
            ├─ diff vs ctx.positions() → SELL 先 BUY 后
            └─ return RawSignal[]
      ▼
orders 表落 PENDING（带 account_group=paper_v53）
      ▼
T 日 18:00 人工审核 /orders?date=T+1
T+1 09:10 集合竞价前 client 自动下单
```

## 3. 算法与月末调仓（Section a）

### 算法（双层 inv_vol，无 ERC）

```
第一层（象限内）:    w_inner[i, q] = (1/vol[i]) / Σ_{j∈q}(1/vol[j])
象限收益序列:        r_q[t] = Σ_{i∈q} w_inner[i, q] × r_i[t]
第二层（象限间）:    w_outer[q] = (1/vol_q[q]) / Σ_{p}(1/vol_q[p])
最终权重:           w[i] = Σ_{q∋i}(w_outer[q] × w_inner[i, q])
```

风险度量 `vol[i]` = 过去 `risk_lookback` 天日收益标准差（年化与否不影响 inv_vol 结果）。

### QUADRANT_MAP（按 handoff 第 1 节）

```yaml
quadrants:
  growth_up:    [hs300, cyb, gold, commodity2, commodity3, sp500, nasdaq, dividend]
  growth_down:  [bond, gold]
  inflation_up: [gold, commodity2, commodity3, crude_oil]
  inflation_down: [hs300, bond, dividend]
```

- `gold` 在 3 象限、`hs300/bond/dividend` 各在 2 象限 → "重复计入"是设计意图（v53 原计划），**不去重**
- `min_history_days = 126`（同 v48 默认），不够的资产从当次调仓的象限内自动剔除

### ETF code 映射

| 内部 key | QMT code | 中文 |
|---|---|---|
| hs300 | 510300.SH | 沪深300 |
| cyb | 159915.SZ | 创业板指 |
| bond | 511260.SH | 10Y 国债 |
| gold | 518880.SH | 黄金 |
| commodity2 | 159981.SZ | 能源化工 |
| commodity3 | 159985.SZ | 豆粕 |
| crude_oil | 159930.SZ | 原油 |
| sp500 | 513500.SH | 标普500 |
| nasdaq | 513100.SH | 纳斯达克100 |
| dividend | 512890.SH | 红利低波100 |

**注**：handoff 提的 5Y/10Y bond 桥接（511260/511010）**生产不需要** —— 511260 上市 2017-08，覆盖 v53 起跑日 2019-12-05 之后的全部窗口。

### 月末判断

零新依赖。Adapter 自己从 IngestService 的 `510300.SH`（沪深300 ETF，2012-05 至今每个交易日都有）trade_date 序列推："`target_date` 所在月份的所有 trade_date 中，`target_date == max`"。

```python
def _is_month_end(self, ctx, target):
    df = ctx.market("510300.SH")  # 已有的 ETF
    if df.empty: return False
    same_month = df[df["trade_date"].dt.month == target.month]
    same_month = same_month[same_month["trade_date"].dt.year == target.year]
    if same_month.empty: return False
    return target == same_month["trade_date"].max()
```

## 4. 数据 bundle 与刷新（Section b）

### bundle 文件

| 文件 | 来源 | 上传 | 大小 | 白名单 |
|---|---|---|---|---|
| `etf_close.parquet` | Mac 本地 `/Users/mameican/Desktop/策略复现/data/market/daily/etfs/` × 10 拼接 | `POST /admin/upload-data` | ~150KB | ✓ |
| `etf_meta.parquet` | 手工生成 | 同上 | <10KB | ✓ |

**`etf_close.parquet` schema**: `trade_date(datetime), code(str), close(float), open(float)`  
**`etf_meta.parquet` schema**: `code(str), name(str), list_date(date), is_qdii(bool), quadrants(list[str])`

### `V53Adapter.data_files` 白名单

```python
class V53Adapter(Strategy):
    name = "v53"
    data_dir = _V53_DIR / "data"
    data_files = ["etf_close.parquet", "etf_meta.parquet"]
```

### 调仓时数据拼接

```python
def _build_returns_matrix(self, ctx, target):
    # 1. 从 bundle 读 close 序列
    bundle_df = pd.read_parquet(_V53_DIR / "data" / "etf_close.parquet")
    bundle_end = bundle_df["trade_date"].max()

    # 2. 对每个 ETF code，从 IngestService 取 bundle_end+1 到 target 的增量
    pieces = []
    for code in self._etf_codes():
        incr = ctx.market(code)
        if incr is not None and not incr.empty:
            incr = incr[(incr["trade_date"] > bundle_end) & (incr["trade_date"] <= target)]
            if not incr.empty:
                pieces.append(incr[["trade_date", "code", "close"]])
    
    # 3. 拼成 wide format
    combined = pd.concat([bundle_df, *pieces]) if pieces else bundle_df
    close_wide = combined.pivot(index="trade_date", columns="code", values="close")
    close_wide = close_wide.sort_index().loc[:target]
    
    # 4. 取最近 risk_lookback (252) 天算日收益
    close_window = close_wide.tail(self._cfg.risk_lookback + 1)
    returns = close_window.pct_change().dropna(how="all")
    return returns
```

### bundle 刷新节奏

每月调仓前一周内 Mac 本地跑 `refresh_v53_bundle.sh`：
1. `cd /Users/mameican/Desktop/策略复现 && git pull`（如果用同事 GitHub 作为数据源）或本地 daily 数据 sync 后
2. 生成 `etf_close.parquet`（10 ETF 全历史）
3. `curl -X POST .../admin/upload-data -F file=@etf_close.parquet -F strategy_name=v53 -F filename=etf_close.parquet`

月度 cadence 足够：v53 没有 ML 模型，bundle 只是历史 OHLCV，几个月不刷也能跑（IngestService 会补增量），月刷只是 hygiene。

## 5. Adapter 接口与风控钩子（Section c）

```python
class V53Adapter(Strategy):
    name = "v53"
    data_dir = _V53_DIR / "data"
    data_files = ["etf_close.parquet", "etf_meta.parquet"]

    def run(self, ctx, trade_date):
        target = parse_yyyymmdd(trade_date)

        if not self._is_month_end(ctx, target):
            return []

        try:
            self._load_resources()
        except Exception as e:
            logger.warning("V53 资源加载失败: %s", e)
            return []

        returns = self._build_returns_matrix(ctx, target)
        if returns.shape[0] < self._cfg.min_history_days:
            logger.warning("V53 历史数据不足 (%d < %d)，跳过", returns.shape[0], self._cfg.min_history_days)
            return []

        target_weights = vendor_compute_baseline(
            returns,
            quadrants=self._cfg.quadrants,
            method="inv_vol",
            risk_lookback=self._cfg.risk_lookback,
            min_history=self._cfg.min_history_days,
        )

        # NAV 计算
        ctx_positions = ctx.positions()
        position_value = sum(
            qty * self._latest_close(ctx, code)
            for code, qty in ctx_positions.items()
        )
        nav = ctx.cash() + position_value

        # 权重 → 100 股整
        target_qty = {}
        for internal_key, w in target_weights.items():
            qmt_code = ETF_CODE_MAP[internal_key]
            ref_price = self._resolve_reference_price(ctx, qmt_code, returns, target)
            if ref_price is None or ref_price <= 0:
                continue
            lots = round(nav * w / ref_price / 100)
            if lots > 0:
                target_qty[qmt_code] = lots * 100

        # 风控钩子
        target_qty = self._apply_risk_filters(ctx, target_qty, target)

        # diff vs current
        signals = self._diff_and_emit(ctx, ctx_positions, target_qty)

        # dry-run 模式
        if self._cfg.dry_run:
            logger.info("V53 DRY-RUN target_qty=%s signals=%d", target_qty, len(signals))
            return []

        return signals
```

### `config.yaml` 风控默认

```yaml
strategy_id: v53
algorithm: inv_vol            # 不用 erc
rebal_schedule: month_end
risk_lookback: 252
min_history_days: 126
dry_run: true                 # M0 阶段；M1 切 false

quadrants:
  growth_up:    [hs300, cyb, gold, commodity2, commodity3, sp500, nasdaq, dividend]
  growth_down:  [bond, gold]
  inflation_up: [gold, commodity2, commodity3, crude_oil]
  inflation_down: [hs300, bond, dividend]

risk_filters:
  qdii_premium_threshold: 0.05    # 513500/513100 溢价 > 5% → skip
  liquidity_multiplier: 100       # 当日成交量 < 100 × 目标买入量 → skip
  max_single_etf_weight: 0.75     # 单 ETF 上限（防双层 inv_vol 极端组合 bond > 75%）

reference_price:
  prefer: ctx_market              # 同 v20h，优先 IngestService 最近真实 close
  fallback: bundle_close          # 回退 bundle 内 close
```

### 已知开放问题（spec 上线前澄清）

**O1. QDII IOPV 数据源**：QMT 能拉 ETF 实时 IOPV 吗？  
- 如果能：风控钩子 (a) 取 IOPV，计算 `(close - IOPV) / IOPV` > 0.05 则 skip
- 如果不能：fallback 用"当日 close vs 过去 20 日均值"做近似溢价指标，或暂时关闭该钩子，写 TODO 进 handbook  
- **行动**：实施前查 [迅投 XtData 文档](C:\parttime\qmt数据推送\201. XtQuant.XtData 行情模块 _ 迅投知识库.md)

**O2. 同事 master 算法 sync 机制**：`refresh_v53_bundle.sh` 是否同时 sync 同事 GitHub 的 `weight_methods.py` 等 vendor 代码？  
- 选项 A：是（每月刷 bundle 时连算法一起 git pull → 拷贝 → push 到 server）→ 算法漂移可见
- 选项 B：否（vendor 只在初次集成时拷贝，之后冻结。如果同事改了算法需要人工 review + PR）→ 推荐  
- **行动**：实施时定 B，写进 handbook

**O3. v20h `owned_symbols` 校验**：v20h 用 `owned_symbols=None` legacy 模式，如何防止漏配？  
- 实施时加 `ReconcileService.validate_no_overlap()`：启动时检查 strategies.yaml 里所有 owned_symbols 不重叠，不需要在 QMT 当前持仓里全覆盖（动态新持仓允许）  
- **行动**：实施时加这个 startup hook

## 6. Reconcile 改造（Section d）

### `app/models/instance_state.py`

```python
class InstanceState(Base):
    instance_id: str               # PK
    virtual_cash: float
    virtual_positions: JSON        # {symbol: qty}
    owned_symbols: JSON | None     # ← NEW: list[str] 或 null
    strategy_state: JSON | None
    last_update: datetime
```

Migration:
```sql
ALTER TABLE instance_state ADD COLUMN owned_symbols TEXT;  -- JSON
```

### `strategies.yaml`

```yaml
account_groups:
  - group_id: paper_v20h
    qmt_account_id: "301300148788"
    strategies:
      - strategy_id: v20h_v1_3
        virtual_initial_cash: 10000000
        # owned_symbols: null (legacy, 排除 others 后剩下的)

  - group_id: paper_v53
    qmt_account_id: "301300148788"      # 同一 QMT 账户
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

Loader 读 strategies.yaml → 写入 `instance_state.owned_symbols`。复用现有 strategies.yaml → instance 加载路径（参考 `app/settings.py` / 现有 `account_groups` parser），仅追加 `owned_symbols` 字段映射。

### `reconcile.py` 改造

```python
def reconcile(self, snapshot, initial_cash=None):
    with self.session_factory() as session:
        inst = session.get(InstanceState, snapshot.instance_id)
        if inst is None: raise InstanceNotFound(...)

        my_owned = inst.owned_symbols  # list[str] or None
        
        # 计算"其他 instance 的 owned"集合（仅当 my_owned is None 时用）
        others_owned: set[str] = set()
        if my_owned is None:
            for other in session.query(InstanceState).filter(InstanceState.instance_id != snapshot.instance_id):
                if other.owned_symbols:
                    others_owned.update(other.owned_symbols)
        
        # 过滤 QMT positions
        qmt_positions: dict[str, int] = {}
        for s, q in snapshot.qmt_positions.items():
            q_int = int(q)
            if q_int <= 0: continue
            if q_int > MAX_REASONABLE_QTY_PER_STOCK:
                logger.warning("filtered outlier %s qty=%d", s, q_int)
                continue
            if my_owned is not None:
                if s in my_owned: qmt_positions[s] = q_int
            else:
                if s not in others_owned: qmt_positions[s] = q_int
        
        # 后续 diff 逻辑保留现有 ── 用 qmt_positions 替代 raw
        # ……（不变）

        # ⚠️ Cash 改造：不再强对齐
        # 旧逻辑：apply 模式下 inst.virtual_cash = snapshot.qmt_cash
        # 新逻辑：positions 强对齐照旧；cash 由 settlement service 自己维护
        if not snapshot.dry_run:
            inst.virtual_positions = dict(qmt_positions)
            inst.last_update = _now_iso()
            # NOTE: virtual_cash 不动
            session.commit()
        
        return result
```

### 新增 `reconcile_cash_total()`

```python
def reconcile_cash_total(self, qmt_total_cash: float, tolerance: float = 0.05):
    """检查 Σ(virtual_cash) ≈ QMT total cash。仅报警，不修改。"""
    with self.session_factory() as session:
        instances = session.query(InstanceState).all()
        total_virtual = sum(inst.virtual_cash for inst in instances)
        deviation = abs(qmt_total_cash - total_virtual) / max(total_virtual, 1)
        if deviation > tolerance:
            logger.warning(
                "cash_total mismatch: virtual=%.2f qmt=%.2f deviation=%.2f%%",
                total_virtual, qmt_total_cash, deviation * 100,
            )
            # 触发微信报警（接现有 alert 通道）
            return False
        return True
```

每日 17:00 跑（settlement 之后），独立 cron。

### 启动校验

`ReconcileService.validate_no_overlap()`：

```python
def validate_no_overlap(self):
    """启动时检查 strategies.yaml 里所有 owned_symbols 不重叠。"""
    all_owned: dict[str, str] = {}  # symbol → instance_id
    with self.session_factory() as session:
        for inst in session.query(InstanceState).all():
            if not inst.owned_symbols: continue
            for s in inst.owned_symbols:
                if s in all_owned:
                    raise OwnershipOverlap(
                        f"symbol {s} owned by both {all_owned[s]} and {inst.instance_id}"
                    )
                all_owned[s] = inst.instance_id
```

`app/main.py` 启动时调用。

## 7. Ops 配套（Section e）

### Dashboard 扩展

`app/api/dashboard.py`：
- 总览页：加 "instances 汇总卡"（合并 NAV + 各 group 占比）+ 各 instance NAV trend 双线对比
- 单 instance 详情页：v53 加 "目标权重 vs 实际权重 diff" 图块（每月调仓后用）

### `V53_OPERATIONS_HANDBOOK.md`

仿 `V20H_OPERATIONS_HANDBOOK.md` 结构，新增以下 v53 专属章节：

1. 一句话总览（10 ETF 全天候 + 双层 inv_vol + 月末调仓）
2. 算法机制详解（QUADRANT_MAP、min_history 截断、ETF 元数据）
3. 数据依赖（bundle 文件、上传命令、月度刷新流程）
4. 风控参数（QDII / 流动性 / max_single_etf_weight 含义与调参指南）
5. 月末调仓 SOP（T-2 刷 bundle、T 16:00 trigger、T 18:00 眼检 /orders、T+1 09:10 集合竞价）
6. Reconcile 排错（owned_symbols 漏配、cash 总量偏离、QMT 共享账户冲突）
7. 已知 trade-offs 速查（handoff 第 5 节复述）

### 部署门槛

**M0 — dry-run（1 个月末 cycle，~30 天）**

- [ ] vendor 代码**直接拷贝**入 `plugins/v53/vendor/`（不用 git submodule —— 避免外部 repo 依赖 + 算法版本永远跟随 server commit）
- [ ] `strategy.py` + `v53_adapter.py` 实现
- [ ] `etf_close.parquet` + `etf_meta.parquet` bundle 生成 + 上传
- [ ] `strategies.yaml` 加 paper_v53 group（`dry_run: true`）
- [ ] `instance_state` migration
- [ ] `reconcile.py` 改造 + `validate_no_overlap()` startup hook
- [ ] 单元测试：`_is_month_end` / `_build_returns_matrix` / 权重计算与 reference_config 对照 / RawSignal 输出 schema
- [ ] e2e：v53 走完一遍 trigger_pipeline → 非月末 adapter return []，月末 dry_run 模式下也 return [] 但 log 写出目标权重和 diff，orders 表无新条目
- [ ] 跑 1 个月末 cycle，确认月末当天 adapter log 算出的权重 ≈ handoff 附录 A 持仓画像（bond ~67% / dividend ~8.7% / hs300 ~6% / 商品合计 ~14%）

**M1 — paper 实盘（无限期）**

- [ ] M0 通过 → `dry_run: false`
- [ ] 第一次月末当天人工 18:00 拉 `/orders?date=T+1&account_group=paper_v53` 眼检，约 10 笔 BUY
- [ ] 集合竞价前 client 自动下单
- [ ] 每日 17:00 reconcile_cash_total 检查；每月调仓后第二天人工查 NAV
- [ ] 跑 3+ 个月 cycle，实测月度回报偏离回测预期 ±2σ 即可

**M2 — 实盘真钱** ── 本 spec 不覆盖

### 跨策略资金冲突预防

- `virtual_cash` server 侧各自独立，**不冲突**
- QMT 真实 cash 共享：第一次 v53 月末建仓 ~1000 万买入 → M1 上线前必须确认 QMT 账户 cash + 可用资金 ≥ v53 需求 + v20h 当日占用
- `reconcile_cash_total()` 每日报警（前面 §6 已述）

### Mac 本地脚本（不在 server repo）

`refresh_v53_bundle.sh`（建议放 `/Users/mameican/Desktop/策略复现/scripts/`）：
```bash
#!/usr/bin/env bash
set -euo pipefail
cd /Users/mameican/Desktop/策略复现
# 1. 拉取/更新数据（如果用同事 GitHub）
# git pull
# 2. 生成 etf_close.parquet
python scripts/build_v53_bundle.py
# 3. 上传到 server
curl -X POST \
  -H "Authorization: Bearer $QMT_API_KEY" \
  -F "files=@./out/etf_close.parquet" \
  -F "files=@./out/etf_meta.parquet" \
  "$QMT_SERVER_URL/admin/upload-data?strategy_name=v53"
echo "✅ v53 bundle refreshed"
```

## 8. 风险与失败模式

| 风险 | 触发条件 | Mitigation |
|---|---|---|
| Bundle 数据漂移 vs 同事 master | 长期不刷 bundle，同事可能修了 weight_methods | 月度 refresh + handbook 写明算法变更需 PR |
| QMT cash 不够 v53 月末建仓 | M1 第一次月末，v20h 占用大量 cash | 月末调仓前 1 天人工检查 + reconcile_cash_total 报警 |
| owned_symbols 漏配 | v53 加新 ETF 后 strategies.yaml 没更新 | `validate_no_overlap()` 启动校验 + 月度 reconcile 报告 |
| QDII 溢价数据缺失 | QMT 不提供 IOPV | 实施前确认 (O1)；缺失则关闭钩子并写 TODO |
| 月末判断错位 | IngestService `510300.SH` 数据缺失 | adapter 回退：用 calendar fallback (硬编码 trading_calendar 或单独 bundle) |
| dividend 双象限的"虚高权重"被外部质疑 | 投资人/同事看月报问 dividend 为啥 8.7% | handbook 写明 v53 设计意图，链接 handoff 第 5 节 |
| 算法实测 OOS 显著差于回测 | M1 跑 3 个月发现 Sharpe < 0.8 或回撤 > -10% | 走 v53 vs v48 walk-forward 验证（handoff Q5 提到的延伸研究） |

## 9. 测试策略

### 单元测试 `plugins/v53/tests/`

- `test_is_month_end.py`: 边界（月初、月中、月末、跨月、月末是周末顺延）
- `test_build_returns_matrix.py`: bundle 完整 / 部分覆盖 / 全 IngestService 增量 / 缺数据
- `test_compute_baseline.py`: 跟 vendor `reference_config.py` + 同事提供的 reference output 对比，权重差 < 1e-6
- `test_risk_filters.py`: QDII 溢价 / 流动性 / max_single_etf_weight 单独触发

### 集成测试 `v2.3/server/tests/integration/`

- `test_v53_pipeline_e2e.py`: 跑一次 trigger_pipeline → 检查 adapter return [] (非月末) / 返回正确数量 RawSignal (月末)
- `test_reconcile_multi_instance.py`: 模拟 QMT 推送含 v20h 股票 + v53 ETF 的 snapshot，分别 reconcile 各 instance，验证白名单过滤正确
- `test_cash_total_check.py`: virtual_cash 总和 vs QMT total，超阈值触发报警

### 对照测试（与同事 master 对比）

- 用同事 v53/main.py 跑出来的 weights.csv（任一历史月末调仓日）作 fixture
- server 端拼相同数据 → 算 weights → diff 应 < 1e-6
- 跑 6 个历史月末 cycle，全部通过才允许 M1 切实盘

## 10. 实施顺序（writing-plans 阶段细化）

粗粒度 step 顺序，writing-plans 会拆成可执行任务列表：

1. vendor 算法核心（`weight_methods.py` + `erc_solver.py` + `reference_config.py` 拷入 + 跑通对照测试）
2. `strategy.py` 算法薄壳实现 + 单测
3. bundle 生成脚本 `scripts/build_v53_bundle.py`（Mac 本地） + 上传走通
4. `v53_adapter.py` 实现（含月末判断 + 数据拼接 + NAV + 风控 + diff）
5. `instance_state` schema migration + `strategies.yaml` 加 paper_v53
6. `reconcile.py` 白名单过滤 + `validate_no_overlap` + `reconcile_cash_total`
7. Dashboard 扩展（multi-instance 视图）
8. `V53_OPERATIONS_HANDBOOK.md` 写完
9. M0 部署 + 跑 1 个月末 cycle 观察
10. M1 切 `dry_run: false`

## 11. Out-of-scope（写明，避免 scope creep）

- M2 实盘真钱
- 期货对冲（v20h 也仍 skip）
- 自动 sync 同事 master 算法（手工 PR 模式）
- v53 算法本身的优化或回测（用同事既有结论）
- v48 / v54 等其他全天候变种
- 多账户切分（如果未来要把 v53 拆到独立 QMT 账户，需另开 spec）
