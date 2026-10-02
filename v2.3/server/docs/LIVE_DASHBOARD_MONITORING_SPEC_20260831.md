# 实盘 Dashboard 与 24h 监控指标规范

日期：2026-08-31  
范围：QMT 多策略服务器、MiniQMT 客户端、执行与影子账本  
原则：监控只读；没有采集的数据必须显示为 `missing`，不得显示为健康的 `0`。

## 1. 行业与开源项目调研结论

### 值得借鉴的开源项目

| 项目 | 值得借鉴 | 不直接照搬的原因 |
|---|---|---|
| [QuantConnect LEAN](https://github.com/QuantConnect/Lean) | 订单事件时间线、资产价格叠加委托事件、组合保证金、运行日志、券商连接事件 | 产品面向多资产/多券商，本项目需要适配 QMT 与 A 股时段 |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | `ACTIVE / REDUCING / HALTED` 交易状态、前置风控、重复/过量成交保护、持续对账 | 是交易引擎而非可直接嵌入的 Dashboard |
| [Freqtrade / FreqUI](https://github.com/freqtrade/freqtrade) | 成交率、拒绝信号、订单超时、Sharpe/Sortino/Calmar、回撤区间和持续时间 | 偏加密货币，24×7 市场结构与 A 股不同 |
| [freqdash](https://github.com/ecoppen/freqdash) | 多实例集中监控、P&L/组合/K 线与成交点位 | 项目仍小，且 GPL 代码不应直接复制到当前代码库 |

本次前端只借鉴信息架构和可观测性思想，没有复制这些项目的代码或视觉资产。

### 一致的行业模式

1. 第一屏回答“现在是否安全、是否可交易、哪里需要人处理”，长期绩效放到二级页面。
2. 订单必须展示完整异步生命周期，不能只显示最终成交；QuantConnect 的实盘结果页明确展示提交、
   更新、部分成交、成交和撤单事件。
3. 风控必须位于订单路径上，并具有明确交易状态；NautilusTrader 将风险引擎放在提交/修改路径，
   同时区分 `ACTIVE`、`REDUCING`、`HALTED`。
4. 在线服务至少监控请求量、错误和延迟；离线/批处理管线监控输入、输出、在途数量、最后成功时间
   和端到端心跳。这与 [Prometheus instrumentation guidance](https://prometheus.io/docs/practices/instrumentation/)
   一致。
5. 告警应面向需要人工动作的症状，并能从总览下钻到原因；Dashboard 的行顺序应跟随数据流，参考
   [Grafana dashboard best practices](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/)。
6. 预设资本/信用阈值、异常价格或数量、重复订单、受限证券和即时成交报告是成熟市场接入控制的基本
   要素，参考 [SEC Rule 15c3-5 FAQ](https://www.sec.gov/rules-regulations/staff-guidance/trading-markets-frequently-asked-questions/divisionsmarketregfaq-0)。

## 2. 24h 值守页信息层级

### 第一层：必须 10 秒内读懂

| 指标 | 口径 | 当前状态 |
|---|---|---|
| Trading state | `ACTIVE / REDUCING / HALTED / SHADOW` | 待采集；当前只读页不提供控制按钮 |
| QMT connection | 连接状态、最近成功查询、断线持续时间 | P0 缺口 |
| Market data age | `receive_ts - exchange_ts` 的 p50/p95/p99、最后 tick 年龄 | P0 缺口；当前只有 EOD 分区日期 |
| NAV / Day P&L | 盘中 raw mark-to-market；EOD 必须单独标识 | 当前只有 EOD |
| Gross / net exposure | 按持仓与价格盯市后的总/净暴露 | EOD 已覆盖；盘中仍为 P1 缺口 |
| Current drawdown | 当前 NAV 相对高水位 | 已有 EOD 口径 |
| Critical alerts | 账实不一致、硬限额失败、断线、数据陈旧、重复/孤儿成交 | 部分已有 |

### 第二层：风险和限额消耗

| 类别 | 指标 |
|---|---|
| 资本 | cash、cash ratio、gross/net exposure、单标的/行业集中度、可用资金、可卖持仓 |
| 尾部 | current/max drawdown、20D/60D 波动、VaR 95/99、Expected Shortfall 95/99 |
| 因子 | beta、行业/风格暴露、风险贡献、协方差条件数、压力场景损失 |
| 限额 | 单笔名义、当日买/卖/换手名义、价格保护 bp、拒单率、限额利用率 |
| 流动性 | spread、ADV20、订单参与率、预计清仓天数、ETF IOPV 折溢价 |

VaR/ES 只用于监测和比较，不应单独作为自动停机条件。样本不足时返回 `null`，前端显示 `—`。

### 第三层：执行质量

| 指标 | 计算 |
|---|---|
| Submit → ACK | `broker_ack_ts - submit_ts`，展示 p50/p95/p99 |
| ACK → first fill | `first_fill_ts - broker_ack_ts` |
| Fill rate | `(FILLED + PARTIAL) / submitted`，同时展示绝对数量 |
| Reject / cancel / timeout | 各状态数量、比例和标准化 reason code |
| Execution shortfall | 按方向计算成交 VWAP 相对 arrival raw price 的 bp，并按成交金额加权 |
| Limit utilization | 实际最差价格偏移 / 配置的价格保护上限 |
| Participation | 成交量 / 同期市场成交量 |
| Reconciliation | order/fill/position/cash 的差异数量、金额和持续时间 |

### 第四层：系统与数据链路

- QMT 客户端、server、行情上传器、scheduler、SQLite/存储分别上报心跳时间戳。
- API 请求量、错误率、p50/p95/p99 延迟和在途请求。
- 每个批处理阶段记录最后开始、最后成功、持续时间、输入/输出行数、hash 和 producer commit。
- 告警必须含 `category / severity / instance / as_of / observed / threshold / runbook`。
- Prometheus 标签保持低基数；`order_id`、`symbol` 等高基数维度进入日志/追踪，不作为常驻指标标签。

## 3. A 股时段化值守

| 时段 | 首页必须突出 |
|---|---|
| 隔夜 | 昨日 EOD 完整性、数据包 hash、公司行动、任务最后成功、次日 target 状态 |
| 08:45–09:25 | QMT 连接、账户/持仓/现金对账、行情首包、target 完整性、交易状态 |
| 09:30–11:30 / 13:00–15:00 | tick 年龄、订单 ACK/成交延迟、挂单、拒单、盘中暴露、P&L、限额消耗 |
| 11:30–13:00 | 上午订单终态、未成交原因、现金/持仓临时对账 |
| 15:00–18:00 | QMT 权威回报、孤儿成交、账本分叉、三层 NAV 差、执行归因、不可变日报 |

时段判断必须最终接入交易日历。仅依靠工作日和时钟的判断必须标注 `calendar_aware=false`。

## 4. 告警优先级

### P0：立即处理 / 可阻断新单

- QMT 在可交易时段断线或账户查询连续失败；
- target 数量、权重和、allowlist 或 hash 校验失败；
- 账实现金/持仓不一致；
- 订单超过资本、数量、可卖持仓或价格保护硬限额；
- 重复订单、过量成交、未知终态、孤儿成交；
- market tick / account snapshot 超过经实盘验证的最大允许年龄；
- 当日累计名义金额、订单数或换手超过配置限额。

### P1：告警但默认不自动停机

- fill rate、shortfall、spread、participation、技术现金或跟踪偏离异常；
- 行业集中度、风险贡献、协方差条件数或压力损失突变；
- 滚动波动、VaR/ES 或 drawdown 超过观察带；
- 快照冻结、数据行数突变、独立价格源差异。

P1 阈值应先用至少 3–6 次真实调仓建立分布，再固定为业务阈值，不能用主观数字直接上线。

## 5. 当前交付与下一阶段

当前已实现 `/admin/ops/live-snapshot`、`/admin/metrics/daily-risk` 和新的
`Live Command Center`：

- EOD NAV、日 P&L、当前回撤、20D 波动、历史 VaR 95、Expected Shortfall 95；
- 30D 订单状态、fill/reject rate、提交/待成交/成交名义、执行 shortfall、ETF premium、费用；
- 执行分析按实例过滤，通过 `order_signal_map` 追溯策略信号，逐单展示 raw 策略参考价、
  委托限价、实际成交 VWAP、方向调整价差 bp 与 implementation cost；
- 价格保护利用率、僵尸 PENDING、账本分叉、快照完整性、隔夜仓位异常；
- CSI 1000 / CSI 300 基准切换、资金流调整收益、累计超额、beta、tracking error、
  information ratio；
- 每日 long/short/gross/net 市值与 exposure、cash ratio、价格覆盖率、陈旧/缺失价格数；
- 按绝对盯市市值排序的持仓和 NAV 权重、最近订单生命周期、告警和行情 EOD 新鲜度；
- 日终持仓优先使用当日收盘价，无当日价格时回退到最近历史收盘并显式统计 `stale`；
- 纸面账本在无 journal 时标记 `assumed_zero_paper`，live 账本缺 journal 时标记
  `missing_live`，不会把未知现金流伪装为 0；
- 明确展示尚未接入的 P0/P1 盘中遥测缺口。

下一阶段按顺序实施：

1. MiniQMT 心跳与 `submit_ts / ack_ts / first_fill_ts / final_ts`；
2. 盘中持仓与 raw mark price，补齐 intraday NAV、P&L、gross/net exposure；
3. tick exchange/receive timestamp，补齐 feed age 和端到端延迟直方图；
4. 申万行业、ADV20、spread、IOPV、participation；
5. 固化三层权重、MRC/RC、压力场景与协方差稳定性到 `risk_snapshot`；
6. 告警 sink 接入值班渠道，并为每条 P0 告警绑定 runbook 与确认/关闭闭环。

## 6. 参考资料

- [QuantConnect live results](https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/results)
- [QuantConnect brokerage and margin events](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/event-handlers)
- [NautilusTrader execution and risk engine](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/concepts/execution.md)
- [Freqtrade performance and drawdown metrics](https://github.com/freqtrade/freqtrade/blob/develop/docs/backtesting.md)
- [Prometheus instrumentation](https://prometheus.io/docs/practices/instrumentation/)
- [Prometheus alerting](https://prometheus.io/docs/practices/alerting/)
- [Grafana dashboard best practices](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/)
- [SEC market-access controls FAQ](https://www.sec.gov/rules-regulations/staff-guidance/trading-markets-frequently-asked-questions/divisionsmarketregfaq-0)
