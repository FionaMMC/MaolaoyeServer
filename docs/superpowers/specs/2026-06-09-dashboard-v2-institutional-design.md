# Dashboard v2 — 机构级监控仪表盘 设计文档

- **日期**：2026-06-09
- **作者**：Meican（+ Claude）
- **状态**：设计待评审
- **分支**：`feature/dashboard-v2`

---

## 1. 背景与动机

现有 `GET /dashboard`（`app/api/dashboard.py`，单文件 846 行 HTML + Chart.js CDN，零构建，60s 全量轮询）已是一个能用的 5-tab（概览/收益/风险/策略内部/交易）量化看板，后端有较全的 `/admin/*` 查询与 metrics 端点。

**升级动机来自本周三次事故——它们都「静默」发生、看板没能第一时间暴露：**
1. **05-28 策略管线触发停摆**，到 06-08 共 7+ 交易日零信号，无人察觉。
2. **05-25 服务器 NAV 快照冻结**（daily_return=0、净值克隆自 05-22），污染净值曲线。
3. **06-09 国债 ETF 持仓被券商误操作翻倍**（511260.SH 49,500→99,000），v53 净值虚增 +68%。

结论：最高杠杆的升级不是「更多图表」，而是一个**运营健康 + 对账 + 数据新鲜度 + 告警层**，把这类事故在发生当刻就顶到眼前；再在其上叠加机构级的风险/归因/IC 监控。

## 2. 已确认的设计决策

- **架构**：原地增强**单文件**（保留零构建、API 直接供 HTML + Chart.js、服务器一体部署）。不做独立前端 SPA、不引入构建管线。
- **更新机制**：**分层轮询 + as-of/陈旧度标记**。运营/健康层高频（~15s）并驱动告警；分析/绩效层日频、「检测到新快照才刷新」+ 手动刷新；**每个面板显示数据 as-of 时间，陈旧数据高亮、绝不当作新鲜展示**。
- **告警**：先做**页面 feed + header badge + `/admin/alerts` 端点**；**预留推送 sink（微信，CLAUDE.md 列为本地侧职责），作为后续接**。
- **节奏**：**分三阶段**，本设计文档描述完整三阶段以保证架构连贯，但**实施计划只详做阶段一**。

## 3. 完整设计

### 3.1 常驻 Header「健康 strip」（命令中心，~15s 轮询）

由一个**廉价端点 `/admin/dashboard-meta`** 驱动，单次调用返回：
- **管线状态**：今日 16:00 是否触发、上次 pipeline 运行时间与摘要（signals/orders/skipped-reason）、scheduler 开关。
- **数据新鲜度**：行情 as-of 日期、**pred 陈旧度 lag 天数（>10 琥珀 / >40 红）**、bundle 日期。
- **客户端心跳**：是否在线、last_seen。
- **对账摘要**：server↔QMT 分叉数、现金分叉、**隔夜 |Δqty| 异常**（>阈值即标红——国债翻倍触发器）、未入账分红现金。
- **告警**：critical/warn 计数 → header 红色 badge。
- **账户级**：合并 NAV（v20h+v53，共用 QMT 301300148788）。
- 还返回**版本令牌**（`max_perf_date`、`last_run_id`、`alert_rev`）供分析层判断是否需要刷新。

### 3.2 标签页（现 5 → 6）

| Tab | 内容 | 数据源 |
|---|---|---|
| **组合总览** | 账户级 rollup + 各策略卡（NAV、D/MTD/ITD、超额、当前 DD、Sharpe、状态）；合并净值曲线；v20h↔v53 日收益相关性 | nav-history×instances，新 `/portfolio/overview` |
| **收益与超额** | NAV vs 基准、**累计超额(alpha)曲线**、收益表 D/W/M/QTD/YTD/ITD（策略/基准/超额）、**月度收益热力图**、上下行捕获、月胜率 | metrics/periodic, nav-history |
| **风险** | 实现+滚动波动、Sharpe/Sortino/Calmar、**水下(回撤)图**、VaR/CVaR、beta、**敞口时序(gross/cash%/持仓数/前10集中度)**、**vol-target 状态**(目标vs实现、缩放) | metrics/*, 新 `/risk/*` |
| **归因** ⭐ | 期间归因**瀑布**(基准→选股→行业→现金拖累→成本→分红→组合)、top/bottom 贡献、**GICS 行业主动权重 vs CSI1000**、市值分档贡献、**live prob_top IC vs 5 年分布**(§5 监控——「alpha 引擎是否在工作」) | 新 `/attribution/*`, `/signal-ic` |
| **执行** | 成交+成交率、**滑点(意图→成交 bps)**、换手、交易成本(%NAV/年化)、blotter、**调仓时钟(di/last_rb_idx/next/will_rebal)**——暴露冻结 pred 的 di 停摆 | trade-analytics, orders, strategy-state |
| **运营与对账** 🆕 | **管线运行日志**(每日 signals/orders/skipped——接 05-28 停摆与陈旧 skip)、数据新鲜度详情、**NAV 快照完整性**(冻结/重复/零收益检测——接 05-25)、**对账异常**(server↔QMT 分叉、现金分叉、隔夜翻倍 tripwire、未入账分红)、心跳历史、**时序告警 feed** | 新 `/ops/*`, `/alerts`, bookkeeping-divergence |

### 3.3 更新机制（落地细节）

- `/admin/dashboard-meta`（廉价）：header strip + 运营 tab 每 ~15s 轮询。
- 分析 tab（收益/风险/归因/执行）：**tab 打开时刷新 + 版本令牌变化时刷新**（事件感知）+ 手动刷新按钮。不对日频数据每 15s 猛拉。
- **每个面板「as-of …」+ 陈旧高亮**：例如 pred 33d→琥珀/红；行情非今日→琥珀；快照冻结→红。

### 3.4 告警

运营层每次轮询跑一套**检查（check suite）**，失败项→告警（info/warn/critical）进 feed + header badge。检查至少含：
- 管线今日未触发 / pipeline skipped(stale) → warn/critical。
- 行情陈旧 > N 天 / pred lag > 阈值 → warn。
- NAV 快照冻结（连续相同 / daily_return==0 于交易日）→ warn。
- server↔QMT 持仓分叉 / 现金分叉 > 阈值 → warn/critical。
- **隔夜单标的 |Δqty|/qty > 阈值（默认 0.5）→ critical**（国债翻倍这类）。
- 未入账分红现金 > 阈值 → info。
- 客户端心跳超时 → critical。
端点 `/admin/alerts` 返回当前告警列表（含 severity、as-of、详情）。**推送 sink 预留**：定义 `AlertSink` 接口，本轮只实现 `DashboardSink`（页面）；`WeChatSink` 留空待接。

### 3.5 架构与模块化

- 保留 `GET /dashboard` 单文件服务 + Chart.js CDN（零构建）。
- 把 846 行内联 JS **模块化**：共享层（fetch/格式化/刷新调度器/告警渲染）+ 每 tab 一个 render 模块。可继续内联在单文件的多个 `<script>` 段，或服务器另供几个静态 JS（仍零构建）。目标：单文件别再膨胀成不可维护。
- **大部分工作是新后端端点**，且**尽量复用我们已写的分析脚本逻辑**（§2/§3/§5 的归因、IC、对账）。

新增端点（阶段对应见 §4）：`/admin/dashboard-meta`、`/admin/ops/pipeline-runs`、`/admin/ops/snapshot-integrity`、`/admin/ops/reconcile-anomalies`、`/admin/alerts`、`/admin/attribution/*`、`/admin/signal-ic`、`/admin/portfolio/overview`、`/admin/risk/*`。

## 4. 三阶段拆分

### 阶段一（本轮实施 — 运营/对账 + 健康 strip + 告警）
最高杠杆，能接住本周这类事故。交付：
- `/admin/dashboard-meta`（freshness + pipeline last-run + 心跳 + 对账摘要 + alert 计数 + 版本令牌）。
- `/admin/ops/pipeline-runs`（管线运行日志：从 perf_snapshots/raw_signals/orders 重建每日运行摘要 + skipped 原因）。
- `/admin/ops/snapshot-integrity`（冻结/重复/零收益/缺口检测）。
- `/admin/ops/reconcile-anomalies`（server↔QMT 分叉 + 现金分叉 + 隔夜 |Δqty| 异常 + 未入账分红；复用 bookkeeping-divergence/reconcile 逻辑）。
- `/admin/alerts`（check suite → 告警列表；`AlertSink` 接口 + `DashboardSink`）。
- 前端：常驻 header 健康 strip（15s 轮询 dashboard-meta）+ 新「运营与对账」tab（运行日志/新鲜度/快照完整性/对账异常/心跳/告警 feed）+ as-of/陈旧高亮 + 告警 badge。
- JS 模块化骨架（共享 fetch/format/refresh-scheduler/alert 渲染），供后续阶段复用。

### 阶段二（归因 + IC 监控）
`/admin/attribution/*`（选股/行业/市值/贡献瀑布，复用 QMT export 的 industry_map+index_weights）、`/admin/signal-ic`（port §5 的 rank-IC + 5 年分布 placement）；前端「归因」tab 重做。

### 阶段三（风险 + 组合总览 polish）
`/admin/risk/*`（vol/VaR/beta/敞口/集中度/factor/vol-target 状态）、`/admin/portfolio/overview`（账户级 rollup + 策略相关性）；前端「风险」「组合总览」tab。

## 5. 范围与非目标（YAGNI）

**做（本轮=阶段一）**：运营对账层 + 健康 strip + 告警 feed + as-of/陈旧 + JS 模块化骨架。

**不做**：
- 不做独立前端 SPA / 构建管线（保持零构建单文件）。
- 本轮不做实时 SSE/WebSocket（分层轮询足够；as-of 标记弥补）。
- 本轮不实现微信推送（只留 `AlertSink` 接口 + 页面 sink）。
- 不改鉴权模型（沿用 localStorage API key + verify_api_key）。
- 阶段二/三的归因/IC/风险端点本轮不实现（仅在文档登记）。
- 不动 QMT 真账户 / 不触发交易。

## 6. 待评审中验证的开放点

- `/admin/dashboard-meta` 的「pipeline last-run」如何取：APScheduler 无运行记录表 → 由 raw_signals/perf_snapshots 的当日最新 signal_time/snapshot 推断，或新增一张 `pipeline_runs` 记录表（阶段一可只做推断，登记后续加表）。
- 隔夜 |Δqty| 异常阈值与「正常调仓」区分（调仓日大额变动是正常的；用 will_rebal/换手基线降噪）。
- 心跳数据来源：`/admin/heartbeat` 现有逻辑是否已有 client last_seen 持久化，还是需补。
- 多 worker 下告警 check 的去重（与 scheduler 同：建议单进程；已在 scheduler 修复中处理）。
- 数据新鲜度探针沿用 `…/server/data`（真 store，非 `…/v2.3/data` 陈旧副本）。
