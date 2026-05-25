# V53 全天候 10 ETF 运维手册

**版本**: 1.0 (2026-05-25, M0 dry_run phase)
**目标读者**: Meican (策略主理人) + 搭档 (系统运维)
**适用范围**: `paper_v53_v53` 实例（1000 万模拟盘，与 V20H 共用 QMT 账户 301300148788）

---

## 0. 一句话总览

V53 是 10 个国内 ETF 的全天候组合，每月最后一个交易日按双层 inv_vol 调仓一次。
当前 **Phase M0 dry_run** — 月末算出目标权重但 *不* 发 RawSignal，等观察 1 个月末 cycle 确认决策合理后再切 M1 实盘。

- **资产**: hs300, cyb, bond(10Y), gold, commodity2, commodity3, crude_oil, sp500, nasdaq, dividend
- **算法**: 双层 inv_vol (内 inv_vol → 4 象限 inv_vol)，源自同事 magicboom1/permenant_portfolio master v53/
- **回测表现** (handoff 第 5 节): Sharpe 1.587, 年化 8.79%, 最大回撤 -7.65% (6.5 年)
- **对外预期** (handoff 附录 A 保守版): Sharpe 1.0-1.4, 年化 5-8%, 最大回撤 -5% 至 -10%

---

## 1. 当前状态 (M0 起点)

| 项 | 值 |
|---|---|
| 实例 | `paper_v53_v53` |
| Account group | `paper_v53` (与 paper_v20h 同 QMT 账户 301300148788, server 侧 owned_symbols 隔离) |
| 初始资金 | ¥10,000,000 |
| 持仓 | 0 (首次月末会建 10 个 ETF) |
| 风控参数 | qdii_premium_threshold=0.05, liquidity_multiplier=100, max_single_etf_weight=0.75, min_history_days=126 |
| Bundle | `plugins/v53/data/etf_close.parquet` (26,124 行, 2011-12 ~ 2026-04) |
| dry_run | `true` (M0) |

---

## 2. 策略机制详解

### 2.1 调仓时序 (T 日 16:00 cron, T+1 09:10 集合竞价)

```
T-7~T-1: Mac 本地 /Users/mameican/Desktop/策略复现/scripts/refresh_v53_bundle.sh
         → 重新生成 etf_close.parquet + curl 推 server (POST /admin/upload-data)
         (月度 cadence，跟调仓节奏对齐)

T 日 (=月末最后交易日)
  15:30 → client 推 EOD OHLCV (含 10 ETF, 走 sectors_etfs 板块)
  16:00 → APScheduler 触发 trigger_pipeline.py → server 跑 V53Adapter.run(ctx, T+1)
            判断 T+1 是不是月末? (用 510300.SH trade_date 序列推)
              否 → return [] (绝大多数日子)
              是 → 拼 bundle + IngestService 252 天 close → V53Strategy.compute_targets
                  → NAV → quantity → 风控 → diff → 10-30 笔 RawSignal[]
            dry_run=true: log "V53 DRY-RUN ... would_emit=N" + return []
            dry_run=false: orders 表落 PENDING(account_group=paper_v53)
  18:00 → 你人工拉 /orders?date=T+1&account_group=paper_v53 眼检
T+1 日 09:10 → client order_submit.py 自动下单（如果 dry_run=false 且 orders 表有 v53 条目）
T+1 日 15:00 → 收盘
T+1 日 15:10 → client 推 trade_result + QmtPositionSnapshot → server settlement + reconcile (按 paper_v53 owned_symbols 过滤)
T+1 日 17:00 → reconcile_cash_total 检查总现金偏差 (待 Task 23 部署 cron 后)
```

### 2.2 双层 inv_vol 公式

```
第一层 (象限内):  w_inner[i, q] = (1/vol[i]) / Σ_{j∈q}(1/vol[j])
象限收益:        r_q[t] = Σ_{i∈q} w_inner[i, q] × r_i[t]   (向后偏修复: 用每段实际持仓的权重)
第二层 (象限间):  w_outer[q] = (1/vol_q[q]) / Σ_{p}(1/vol_q[p])
最终权重:        w[i] = Σ_{q ∋ i}(w_outer[q] × w_inner[i, q])
```

vol 用过去 252 天日收益标准差。象限映射 (handoff §1)：

| 象限 | 资产 |
|---|---|
| growth_up | hs300, cyb, gold, commodity2, commodity3, sp500, nasdaq, dividend |
| growth_down | bond, gold |
| inflation_up | gold, commodity2, commodity3, crude_oil |
| inflation_down | hs300, bond, dividend |

注：dividend 在 growth_up + inflation_down 两象限**重复计入**（v53 设计意图，handoff §5 已确认 Meican 接受）。期末权重 ~8.7%。

### 2.3 风控钩子 (config.yaml `risk_filters`)

| 钩子 | 默认 | 触发 | 行动 |
|---|---|---|---|
| qdii_premium_threshold | 0.05 | 513500/513100 收盘 vs 过去 20 日均值 偏离 > 5% | skip 当次该 ETF |
| liquidity_multiplier | 100 | ETF 当日成交量 < 100 × 目标买入量 | skip 该 ETF |
| max_single_etf_weight | 0.75 | 单 ETF 权重 > 75% (防极端 inv_vol) | cap 到 75% |
| blacklist (ctx.risk_blacklist) | — | symbol 在风险黑名单 | skip |

注：QDII IOPV (spec O1) 目前用 20 日均值**近似**。Windows 端确认 XtData 有实时 IOPV API 后可替换。

---

## 3. 数据依赖

| 文件 | 路径 (server) | 谁负责 | 频率 |
|---|---|---|---|
| etf_close.parquet | `plugins/v53/data/` | Meican (Mac local refresh_v53_bundle.sh) | 月度 |
| etf_meta.parquet | 同上 | 同上 | 几乎不变（仅 ETF 上市/退市变化时） |
| ETF OHLCV 增量 | server IngestService (`data/market/daily/etfs/`) | client `market_push.py` (Windows QMT) | 每日 |
| 510300.SH trade_date (anchor for month-end) | 同上 | 同上 | 每日 |

### 上传 bundle 命令

```bash
# Mac 本地
cd /Users/mameican/Desktop/策略复现
export QMT_SERVER_URL=http://<server-ip>:8000
export QMT_API_KEY=<api-key>
./scripts/refresh_v53_bundle.sh
# 内部步骤：python scripts/build_v53_bundle.py → curl POST /admin/upload-data × 2
```

---

## 4. 月末调仓 SOP

**T-7 ~ T-1（调仓周前一周）**:
- [ ] Mac 跑 `refresh_v53_bundle.sh` 刷 bundle
- [ ] 检查 server `GET /admin/data-status?strategy_name=v53` 确认 etf_close.parquet + etf_meta.parquet mtime 是最新

**T 日（月末最后交易日）**:
- 16:00 (cron) — APScheduler 自动跑 trigger_pipeline → V53Adapter.run(ctx, T+1)
- 16:05 — 检查 server log: `journalctl -u qmt-server --since '16:00' | grep V53`
  - 预期看到 `V53[paper_v53_v53] DRY-RUN trade_date=<T+1> nav=10000000.00 target_qty={...} would_emit=10 signals`
  - 对比 handoff 附录 A 持仓画像：bond ~67%, dividend ~8.7%, hs300 ~6%, 商品合计 ~14% — 偏差 > 20% 是异常
- 18:00 — 拉 orders 眼检（仅 dry_run=false 阶段才有条目）:
  ```bash
  curl -s -H "Authorization: Bearer $QMT_API_KEY" \
    "http://<server>:8000/orders?date=<T+1>&account_group=paper_v53" | jq
  ```

**T+1 日**:
- 09:10 集合竞价前 client 自动下单（dry_run=false 阶段）
- 15:10 client 推 trade_result + QmtPositionSnapshot
- 17:00 检查 reconcile 报告: `journalctl -u qmt-server --since '17:00' | grep -E 'reconcile|V53'`

---

## 5. Reconcile 多 instance 排错

V53 + V20H 共用 QMT 账户 301300148788。Server 按 owned_symbols 白名单过滤:
- `paper_v20h_v20h_v1_3.owned_symbols = null` (legacy 模式：消费除 paper_v53 外的全部)
- `paper_v53_v53.owned_symbols = [10 个 ETF code]` (严格白名单)

### 常见问题

**症状**: app 启动 raise `OwnershipOverlap`
- 原因: 两个 instance 的 owned_symbols 列表有重叠（误配）
- 排查: `sqlite3 pipeline-server.db "SELECT instance_id, json(owned_symbols) FROM instance_state;"`
- 修复: 改 strategies.yaml 让 owned_symbols 不重叠 + 重启

**症状**: reconcile 报 `cash_total mismatch`
- 原因: `Σ(virtual_cash)` 偏离 QMT 总现金 > 5%
- 原因 1: 某 instance settlement 漏了某笔成交 → 查 trade_result + bookkeeping_divergence
- 原因 2: QMT 账户有非 V20H 非 V53 的资金/持仓被算进来 → 看 QMT 真实账户清单确认
- 报警仅 log + 微信，不阻塞流程

**症状**: paper_v53 instance 突然丢失某些 ETF 持仓
- 原因: client `query_qmt_positions.py` 推送的 QmtPositionSnapshot 漏了该 ETF（QMT 持仓查询失败）
- 排查: client 日志 + 直接登陆 QMT 客户端核对实际持仓

---

## 6. 已知 trade-offs (handoff §5 复述)

1. **v53 vs v48 Sharpe 差 -0.005 是噪声**（Sharpe SE ≈ 0.17）。加 dividend 没显著改 Sharpe，主要价值在风险多元化
2. **dividend 双象限重复计入** → 期末权重 ~8.7% 比"单算一次"重。Meican 已接受
3. **v54（加可转债）回撤改善 -5.45pp 反而是 6 个加资产版本最大的**，但 Meican 选 v53 (业务理由: 跟同事一致 + 红利因子敞口)
4. **6 个加资产版本 ΔSharpe 全在 Bonferroni 噪声阈值内** → 不要再加资产 (data dredging)
5. **inv_vol vs 真 ERC**: 在 10 资产场景两者差 ~0.03 Sharpe (噪声范围)；用 inv_vol 是有意选择

详见 spec docs/superpowers/specs/2026-05-24-v53-allweather-bottom-integration-design.md §1

---

## 7. M0 → M1 切换 checklist

**前置**: M0 至少跑过 1 个月末 cycle，目标权重画像 ≈ handoff 附录 A (bond ~67% / dividend ~8.7% / ...)。如果偏差大需要排查（数据 / 算法 / 配置）。

```bash
# 1. 在 server 上改 dry_run
ssh deploy@<server>
cd /opt/qmt-server/v2.3/server
vi plugins/v53/config.yaml
# 改 dry_run: true → dry_run: false

# 2. 重启
sudo systemctl restart qmt-server

# 3. T 日 16:00 cron 跑后看 log
journalctl -u qmt-server --since '16:00' | grep V53
# 预期: "V53[paper_v53_v53] go-live trade_date=<T+1> nav=... emitted=10 signals"

# 4. T 日 18:00 拉 orders 眼检
curl -s -H "Authorization: Bearer $QMT_API_KEY" \
  "http://<server>:8000/orders?date=<T+1>&account_group=paper_v53" | jq '.data.orders[] | {symbol,direction,quantity,reference_price}'

# 5. T+1 09:10 集合竞价 client 自动下单
# 6. T+1 15:10 后查 reconcile
```

**M1 → M2 (实盘真钱)** 不在本 spec 范围。需要单独评审 + Meican 明确金额 + 重新评估风险。

---

## 8. 文件 / 模块速查

| 文件 | 作用 |
|---|---|
| `v2.3/server/plugins/v53/vendor/{weight_methods,erc_solver,risk_parity,reference_config}.py` | 同事 master 算法核心 (vendored @ commit e55e0b1, 单向不改) |
| `v2.3/server/plugins/v53/strategy.py` | V53Strategy 薄壳 (close_px + target_date → {QMT_code: weight}) |
| `v2.3/server/plugins/v53/code_map.py` | internal_key (hs300) ↔ QMT_code (510300.SH) 映射 |
| `v2.3/server/plugins/v53/config.yaml` | 策略参数 (dry_run + risk_filters + quadrants) |
| `v2.3/server/plugins/v53_adapter.py` | Strategy 协议适配（run 内 8 步 pipeline） |
| `v2.3/server/plugins/v53/data/etf_close.parquet` | bundle，10 ETF 全历史 (gitignored, refresh_v53_bundle.sh 生成) |
| `v2.3/server/app/services/reconcile.py` | multi-instance reconcile (owned_symbols 过滤 + validate_no_overlap + cash_total) |
| `v2.3/server/strategies.yaml` | account_groups 含 paper_v20h + paper_v53 |

---

**Spec**: `docs/superpowers/specs/2026-05-24-v53-allweather-bottom-integration-design.md`
**Plan (implementation)**: `docs/superpowers/plans/2026-05-24-v53-allweather-integration.md`
**Handoff (algorithm context + trade-offs)**: `/Users/mameican/Desktop/策略复现/HANDOFF_to_server_repo.md`
