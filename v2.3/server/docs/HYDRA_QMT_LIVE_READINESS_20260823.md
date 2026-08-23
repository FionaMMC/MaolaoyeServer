# Hydra → QMT 实盘接入基座审计（2026-08-23）

## 结论

当前状态是 **NO-GO**。Hydra 目标和服务器影子账本可以继续运行，但不得通过修改
`dry_run` 或 `orders_enabled` 直接接入实盘账户。

实盘首要工作不是开启策略，而是建立不可绕过的 paper/live 分域、两条价格数据链、
独立 live client、外部现金流账本和残余目标补单状态机。生产服务器在这些门禁完成前
保持不变。

## 已冻结的基线

- Server/GitHub 基线：`d9af351c53e4dcaf9b8c30ae8c3f6eeb5d2fc8b5`。
- 当前生产 Hydra shadow publisher：
  `66985a19621e9dc8b5f2525e57ba1696fa7a9236`。
- 当前生产 Hydra source version：
  `v48.1-RB@49c16dadc298d6a51470bd5c2f931ecc36f65460`。
- 实盘候选 Hydra 基座分支：`codex/hydra-live-baseline-20260823`。
- 实盘候选 Hydra HEAD：`aa6b60d`；它包含：
  - 未持有、非目标 ETF 缺失开盘价不再污染 NAV；
  - ETF 拆分/合并份额调整的 fail-closed 校验和独立审计日志；
  - 拆分、合并、非法因子、重复事件、未知代码、非交易日、事件日缺价和同日分红测试。

候选 commit 尚未加入 server publisher allowlist，也未替换生产 target。

## 需求单与当前 v2.3 的核对

### 已有且可复用

- `reconcile()`、`reconcile_total()`、shared-ledger 拒绝单实例覆盖等对账能力已经存在。
- 成交回报采用累计成交量/VWAP 语义，支持 `PARTIAL → FILLED` 增量结算和重复回报幂等。
- 已拉取订单具有 `fetched_at` 重算保护。
- pipeline 已有 `orders_enabled`、dedicated/shared-ledger 配置检查和行情新鲜度保护。
- QMT 委托已经携带 server `order_id` 作为 remark，具备自动补录基础。

### P0 阻塞

1. **没有执行域**：`RawSignal`、`Order`、`Trade`、`InstanceState` 和对账记录均无
   `execution_domain`；同一日期的 `/orders` 会返回所有 PENDING 订单。
2. **只有一把 API key**：鉴权不能把 paper client 和 live client 限制在各自执行域。
3. **现有 client 不是 fail-closed**：下单前订单批次复核失败时会继续使用本地旧快照；
   批次变化时会自动替换后继续下单。这两种行为都不允许用于 live。
4. **client 状态未隔离**：配置、SQLite、日志、session、任务名称和 QMT userdata 仍是
   单一运行单元。
5. **ETF 价格口径混用**：普通 `market_push.py` 上传的是前复权 ETF；没有独立的
   Hydra 后复权模型价格和不复权执行价格 contract。
6. **没有外部现金流 journal**：分红、入金、出金和其他非交易现金变化不能与真实账户
   逐项勾稽。
7. **没有月度 residual 状态机**：同月 target 幂等存在，但次日按实际持仓补未成交差额
   尚未形成正式的 target/rebalance/attempt 三层状态。
8. **没有统一 live 订单闸门**：服务端和 Windows 端都缺少按日、按单、按组合的实盘
   硬限额及独立 emergency stop。

## 目标架构

### 1. 执行域和鉴权

新增 `execution_domain = paper | live`，至少贯穿：

- account group / strategy instance；
- raw signal / order / order batch / trade；
- instance state / performance / reconcile snapshot；
- external cash-flow journal / audit bundle。

迁移时所有历史记录默认标为 `paper`。API key 必须解析为带 domain、client id 和允许账户的
认证上下文；domain 由服务端凭据决定，禁止客户端请求参数覆盖。

`GET /orders`、`POST /trade-result` 和对账 API 必须同时按认证 domain、配置账户和日期过滤。
响应必须带 `execution_domain`、`qmt_account_id`、`batch_id`、`batch_sha256` 和 `target_hash`，
live client 对任一不一致值直接退出。

### 2. 双数据链

建议使用独立命名空间，不覆盖现有 `market-data`：

- `hydra_model_hfq`：后复权 OHLC，用于收益、波动率和风险平价权重；
- `hydra_execution_raw`：不复权 OHLC、停牌状态和可交易规则，用于股数、限价、NAV 和对账；
- `hydra_corporate_actions`：现金分红及经过审计的拆分/合并事件。

每批数据必须有 manifest：`source`、`adjustment`、`trade_date`、`fetched_at`、代码覆盖率、
行数、文件 SHA-256 和 producer commit。模型数据与执行数据的共同交易日必须一致；任一链
缺失时不得生成可执行 target。

QMT 作为实盘执行价格主源；Tushare `fund_daily`、`fund_adj`、`fund_div` 用于交叉验证和
受控回补。来源切换必须产生新 manifest，不能静默覆盖。

### 3. Target、月度幂等和次日补单

正式 target 表达目标权重，总权重必须为 1；现金缓冲由执行 contract 单独记录，不能伪装成
ETF。target 最少包含：

`code, weight, decision_date, as_of_date, execution_date, strategy_version,
publisher_source_commit, input_hashes, basket_sha256`。

状态拆成三层：

- `target_id`：月度目标内容；同一内容只消费一次；
- `rebalance_id`：从该 target 和调仓前已对账持仓计算出的目标股数；
- `attempt_id`：T+1、T+2 等订单尝试。

补单不是重新消费 basket。收盘结算后，用实际 QMT 持仓和现金重新对账，再对持久化的目标股数
计算 residual；新 attempt 引用同一 `target_id/rebalance_id`。存在未决委托时禁止生成下一批。

### 4. 独立 live client

新建独立入口和私有配置，禁止复用 paper 的可写状态。启动检查至少验证：

- `execution_domain=live`；
- QMT 账户等于私有环境变量 `HYDRA_LIVE_QMT_ACCOUNT_ID`；
- live 专用 userdata、session id、API key、SQLite、日志和任务名称；
- server 返回账户/domain 与本地完全一致；
- server 和 client emergency stop 均明确处于允许状态。

批次复核失败、网络失败、账户资产查询失败、行情缺失或 hash 不一致时必须不下单。旧 client
“沿用本地快照继续下单”的逻辑不能进入 live 包。

## 资金规模 replay

### 口径

- Hydra `v48.1-RB` 81 个历史月度目标：2019-12-05 至 2026-07-24；
- 每个目标使用下一交易日不复权 ETF 开盘价；
- 100 份一手，目标股数向下取整；
- 预留 1% 现金；
- 只测 lot rounding，尚未加入佣金、0.5% 限价偏移和实时溢价。

ETF 买入 100 份整数倍及基金价格最小变动 ¥0.001 与沪深交易所现行规则一致：
[上交所 ETF 问答](https://www.sse.com.cn/assortment/fund/etf/question/c/c_20240118_5734754.shtml)、
[深交所 2026 年交易规则](https://docs.static.szse.cn/www/lawrules/rule/trade/W020260424690713155663.pdf)。

### 结果

| 目标 | 历史 replay 门槛/结果 |
|---|---:|
| 每月达到至少 80% 的预期标的数 | ¥31,303 |
| 每月所有预期标的至少一手 | ¥54,947 |
| ¥100,000 | 81/81 月完整标的；最低仓位 84.52%；最差单 ETF 权重误差 13.15pp |
| ¥200,000 | 最低仓位 91.87%；最差权重误差 6.43pp |
| ¥500,000 | 最低仓位 96.28%；最差权重误差 2.50pp |
| ¥700,000 | 最低仓位 96.91%；最差权重误差 1.92pp |
| ¥1,500,000 | 最低仓位 98.05%；最差权重误差 0.87pp |

当前目标中 `511260.SH` 权重约 75.5%，一手市值约 ¥13,576。整数手数导致仓位误差随资金
呈锯齿状，并非资金越多就逐点单调改善。因此每次月度调仓都必须用当月权重和实时原价运行
lot-rounding preflight，不能仅依赖一个静态最低金额。

建议资金档位：

- **技术硬下限：¥100,000**。满足历史每月完整标的和至少 80% 仓位，但不算高保真复制。
- **首选实盘灰度：至少 ¥700,000**。历史最差单 ETF 偏差控制在约 2pp。
- **高保真：约 ¥1,500,000**。历史最差单 ETF 偏差控制在 1pp 内。

最终启用金额仍需在当月 T 日收盘后，用 T+1 可交易价、佣金、限价压力和账户真实现金再验算。

## 滑点与 ETF 溢价

每笔 live order 至少记录：

- T 日决策收盘价；
- T+1 订单生成参考价及时间；
- 委托价、价格档位、QMT 委托时间；
- 首笔/末笔成交时间、累计成交量、成交 VWAP；
- 同期盘口或开盘基准；
- 可获得时的 IOPV/iNAV 及其时间戳；
- `decision_gap_bps`、`execution_shortfall_bps`、`premium_bps` 和费用。

买入执行损耗定义为 `(fill_vwap - arrival_reference) / arrival_reference`，卖出方向取反。
决策日至开盘的价格变化单列为 decision gap，不能混入 broker 滑点。ETF 市价相对 IOPV/iNAV
的偏离单列为 premium，不能用复权模型价计算。

需求单中的 0.5% 是价格保护上限，不是默认必须吃掉的滑点。限价还必须按 ¥0.001 tick 对买入
向下/卖出向上做保守舍入，并同时满足交易所有效申报价格范围。QMT `order_stock` 支持限价、
策略名和 remark，`cancel_order_stock` 支持按订单号撤单：
[迅投 XtQuant 交易文档](https://dict.thinktrader.net/nativeApi/xttrader.html)。

## 分阶段交付与放行门禁

### Phase 0 — 研究基座（进行中）

- [x] 冻结生产 server 与 Hydra publisher。
- [x] 选择性吸收缺失开盘价修复。
- [x] 公司行动 ledger 改为 fail-closed 并补核心测试。
- [ ] 产出经过 hash 固定的后复权/原价/公司行动候选数据包。
- [ ] 对新旧 81 月权重、NAV、换手和公司行动逐事件差异审计。

### Phase 1 — Server 分域地基

- [ ] 新模型和向后兼容迁移，历史记录默认 paper。
- [ ] domain-scoped API key 和强制过滤。
- [ ] order batch / target / attempt 持久化。
- [ ] external cash-flow journal。
- [ ] paper/live 交叉访问负向测试。

退出门禁：现有 paper 全量测试不回归，任何跨域读写均失败。

### Phase 2 — Hydra relay 与双数据链

- [ ] 原子数据 manifest 和双链新鲜度检查。
- [ ] Hydra target producer/validator/relay。
- [ ] lot-rounding、价格 tick、白名单、停牌和账户权限预检。
- [ ] residual retry 状态机。

退出门禁：固定输入 replay 可复现相同 basket/order hashes；缺任一数据或血缘字段均不产单。

### Phase 3 — 独立 live client

- [ ] 独立目录、入口、配置、数据库、日志、session、userdata 和任务计划。
- [ ] 双重 emergency stop、日/单/组合硬限额。
- [ ] 批次复核及账户/domain fail-closed。
- [ ] QMT 委托、撤单、累计成交、残余补单和审计包。

退出门禁：mock_qmt 能完整重放，且没有任何真实委托路径被触发。

### Phase 4 — 模拟与灰度

- [ ] 两次同 basket 离线 replay。
- [ ] 专用模拟账户完整月末/月初闭环。
- [ ] live 账户只读初始化和对账演练。
- [ ] 书面验收后小规模实盘；至少覆盖一次完整调仓和一次补单/恢复演练。

## 下一批实现顺序

1. 先做 `execution_domain` 模型、迁移和鉴权上下文，不添加 live 策略配置。
2. 改造 `/orders`、`/trade-result`、reconcile API 及隔离测试。
3. 引入 target/rebalance/attempt 和 external cash-flow journal。
4. 再实现双数据链与 `hydra_relay`。
5. server 门禁稳定后才创建 live client；最后才写入私有账户配置。

任何阶段都不得把真实账号、API key、QMT 路径或密码提交到 Git。

