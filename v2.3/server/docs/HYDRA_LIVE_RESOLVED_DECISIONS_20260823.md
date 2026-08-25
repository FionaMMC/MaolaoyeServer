# Hydra 4.8 → QMT 实盘化：已解决事项与实施方案

> 状态：业务方案已确认；尚未批准连接、查询或操作真实 QMT 账户。

本文记录截至 2026-08-23 已达成一致的业务决策、实现方式及上线前仍需验证的事项。它是对 `HYDRA_QMT_LIVE_READINESS_20260823.md` 与 `HYDRA_LIVE_OPERATIONS_RUNBOOK_20260823.md` 的补充，应作为后续实现和验收的依据。

> 2026-08-25 server implementation note: executable/research-only manifest metadata
> is now preserved and enforced; dynamic `auto` risk is implemented on both server
> and MiniQMT client with immutable per-batch snapshots; strategy cash buffer defaults
> to zero while live share sizing separately reserves 50bp plus 0.1% cost headroom.
> The supplied 2026-08-21-r1 ZIP passed smoke validation only. The unavailable
> `4941029` object was replaced by the independently tested commit
> `8ebfd21a159c74b73397ffb3847878a597d055df`; both research candidate branches now
> point to it. The pushed audit manifest explicitly lists nine referenced artifacts
> that were not received, so the formal 2026-08-31 delivery remains the blocker.

## 1. 基线与范围

| 项目 | 已确认方案 |
|---|---|
| Hydra 研究基线 | `magicboom1/permenant_portfolio` 分支 `codex/hydra-live-baseline-20260823`，提交 `8ebfd21a159c74b73397ffb3847878a597d055df` |
| Server 实盘基线 | `FionaMMC/MaolaoyeServer` 分支 `codex/hydra-live-foundation-20260823`，提交 `c9cb69b938b42ca3bf05f7fa69e592f88b70697f` |
| 首期灰度资金 | ¥200,000 |
| 当前阶段 | mock / dedicated paper 验证；禁止真实 QMT 账户操作 |
| Server 与客户端 | Server 可共享；paper 与 live client 必须彻底分离 |

## 2. ETF 白名单与权重

实盘仅允许 Hydra 4.8 的 9 只 ETF：

```text
510300.SH, 159915.SZ, 511260.SH, 518880.SH, 159981.SZ,
159985.SZ, 159930.SZ, 513500.SH, 513100.SH
```

- 权重完全由 Hydra 4.8 产出；不设置单 ETF 最大权重。
- `511010.SH` 仅用于 `511260.SH` 上市前的研究历史桥接，不得出现在实盘目标、白名单或委托中。
- Server 与 live client 必须各自校验白名单，任何非白名单证券直接拒绝。

## 3. 数据口径与冻结

| 数据流 | 用途 | 冻结来源与口径 |
|---|---|---|
| `hydra_model_hfq` | 收益、协方差、风险平价、目标权重 | QMT 后复权（HFQ）OHLC |
| `hydra_execution_raw` | 目标股数、限价、成交、持仓估值与对账 | QMT 不复权 OHLC、停牌及交易规则 |
| `hydra_corporate_actions` | 分红、拆分、合并及账本处理 | 经审核的公司行动数据，带稳定事件编号 |
| `hydra_trading_calendar` | T 与 T+1 校验 | 冻结交易日历 |

四份数据必须使用同一个 `as_of_date`，且每份都要带不可变 manifest：数据源、复权口径、抓取时间、producer commit、标的覆盖范围、行数与文件 SHA-256。target、sidecar 和 Server 中登记的输入 hash 必须完全一致。

## 4. 限价单与 50bp 价格保护

### 已解决的问题

50bp 不是“预期滑点”，而是每笔订单相对执行参考价的最坏可接受价格带。现有买卖方向和 ¥0.001 tick 取整方向正确，应保留为默认保护规则。

| 方向 | 限价 | 含义 |
|---|---|---|
| 买入 | `reference_price × 1.005`，向下取 ¥0.001 | 最多接受高于参考价 50bp 的成交价 |
| 卖出 | `reference_price × 0.995`，向上取 ¥0.001 | 最多接受低于参考价 50bp 的成交价 |

满足价格条件时，交易所可按更优价格撮合；50bp 不等于实际会产生 50bp 的滑点。

### 实施要求

- 初次调仓与 residual 次日补单均默认使用上述 50bp 保护性限价。
- 永远不得超过 50bp。
- QMT 下单时取得实时到达价，记录为执行质量证据；它不改变已审批的限价。
- 若开盘相对参考价跳空超过价格带，订单可以不成交并留至收盘；不得为了成交而追价突破 50bp。

## 5. 无业务金额上限与技术风控

### 已解决的问题

不按 Hydra 当前 9 标的或首期 ¥200,000 规模写死业务金额、订单数或换手上限，以免未来扩容需要重做设计。

但“没有业务上限”不等于允许无限制下单。`0` 或留空必须继续表示“未获批准”，live 模式 fail-closed；绝不能把 `0` 改成 unlimited。

### 采用方案：动态 `auto` 风控模式

搭档需实现通用的动态技术上限，在每次提交前以 QMT 实际账户快照计算，并将计算结果记入审计记录：

- 单笔名义金额：不超过下单前 QMT 总资产加小幅价格/费用缓冲；
- 当日买入：不超过实际可用现金与已确认卖出回款可覆盖的范围；
- 当日卖出：不超过 QMT 实际可卖持仓；
- 当日总成交额：不超过下单前总资产约两倍加小幅缓冲，覆盖“全卖后全买”的极端调仓；
- 订单数：使用与当前策略无关的通用系统防护阈值，不按 9 只 ETF 写死；
- 仍强制执行：账户/domain/白名单校验、批次 hash、幂等、可用现金、可卖持仓与 50bp 限价保护。

默认配置仍应为未批准即 fail-closed；只有显式启用 `auto` 风控并完成 mock/paper 验收后，live 才可进入后续放行流程。

## 6. 现金、整手与首期资金

- 不设置策略层面的最低现金比例，target 使用 `cash_buffer=0`。
- 技术层面仍须在下单前为佣金、整手取整和价格保护预留可用现金；不得因费用不足产生部分异常下单。
- ETF 买入必须为 100 份整数倍；价格按 ¥0.001 tick 处理；ETF 卖出不计股票印花税。

离线初步实验显示，¥200,000 是可用的首期灰度规模，而不是高保真复制规模：平均最大单 ETF 权重偏差约 3.4pp，历史最差约 6.2pp。正式放行前，必须以冻结的 QMT raw、公司行动与完整 81 个月目标重新计算。

## 7. 日内和跨日订单流程

| 时间 | 必须执行的动作 |
|---|---|
| T 15:10 | 拉取并上报成交、部分成交、拒单及 QMT 状态；拒单在此环节提醒 |
| T 收盘后 | 仅根据 QMT 终态结算；完成现金/持仓对账并关闭 attempt |
| T 16:00 | 仅在不存在未决订单且对账成功后，根据 actual residual 创建 T+1 attempt |
| T+1 09:10 | live client 重新拉取、重算 hash、核对账户和批次后，先卖后买 |

规则如下：

- 未成交订单不主动撤单；让其按交易所/QMT 的收盘终态结束。
- `CANCELLED`、`REJECTED`、`PARTIAL` 必须按 QMT 实际终态记录，不能由本地推断。
- QMT 活动、缺失或未知状态必须阻断结算和补单，并告警；不得伪装为已撤或已废。
- residual 只针对持久化 target/rebalance 的实际差额生成；不得重新消费月度 target。

## 8. 账户隔离、权限与对账

- `execution_domain=paper|live` 贯穿 target、order、trade、reconcile、现金流与审计包。
- paper/live 使用不同 token、client identity、账户别名、账户指纹、userdata、session、SQLite、日志、任务名及可写目录。
- live token 仅拥有 Hydra live 所需的最小 API 权限，且只能访问被授权的账户别名和执行域。
- 下单前必须读取 QMT 总持仓、可卖持仓、可用现金；与 server virtual ledger 清洁对账后才可提交。
- 分红、入金、出金等外部现金变化必须写入幂等 cash-flow journal。
- 任何账号、密钥、密码、私有路径或真实账户标识不得提交 Git。

## 9. 研究、审计与可观测性

研究端交付物：

- 最新 `Hydra_latest.parquet` 及对应 sidecar JSON；
- 完整 publisher commit、策略版本、决策日、`as_of_date`、权重和与研究输入 SHA-256；
- 新旧版本完整 81 个月差异审计：权重、NAV、换手、分红、拆分/合并、缺失行情处理；
- QMT 与独立来源差异报告：价格、停牌、代码映射与复权因子；
- 书面结论：该 commit 和 target 是否批准进入 mock/paper。

每笔成交至少记录决策价、到达价、委托价、成交 VWAP、费用、IOPV/iNAV、溢价、决策跳空与执行短缺。审计包必须包含 target、四份数据 manifest、订单、成交、现金流、对账和执行质量记录，且不得覆盖既有证据。

## 10. 上线前门禁

- [ ] 在生产数据库副本上演练迁移两次，证明迁移幂等，并保留备份。
- [ ] 代码部署后保持所有 live 开关关闭，证明现有模拟盘正常。
- [ ] 同一 basket 完成两次 mock replay，结果 hash 一致，且没有真实 QMT 调用。
- [ ] 专用模拟账户完成一次完整月末/月初闭环。
- [ ] 完成 live 账户只读初始化与对账演练。
- [ ] 演练部分成交、拒单、未决订单、收盘 residual、次日补单、急停与审计导出。
- [ ] 使用冻结 QMT 数据重做 81 个月、¥200,000 的正式资金规模与跟踪误差验证。
- [ ] 业务书面批准后，才可用 ¥200,000 进入小规模实盘灰度。
- [ ] 第一次完整月度调仓结束后，审阅跟踪误差、成交质量、现金流与对账，再决定是否增资。

## 11. 当前待搭档交付的优先事项

1. 合并研究端月末发布保护，并固定正式 Hydra 发布基线；不得把 2026-08-21 smoke target 当作正式 target。
2. 实现并测试动态 `auto` 风控模式，同时保留未配置即 fail-closed 的默认语义。
3. 把 15:10、15:30、16:00、18:00、T+1 09:10 流程接入 Windows 正式任务编排。
4. 完成 mock replay、专用模拟账户月末/月初闭环、部分成交/拒单/未决订单/residual/恢复演练。

## 12. 2026-08-24 研究端补齐结果

- 原 `20260821` QMT smoke ZIP 保持不可变；另生成 `20260821-r1` 研究修订包，补齐 `511010.SH` 的 HFQ 历史桥接。该标的被标记为 `research_only`，不在 raw 包、live 白名单、target 或订单中。
- 同一冻结 QMT HFQ 输入下，旧版 `49c16dadc298d6a51470bd5c2f931ecc36f65460` 与新版 `aa6b60deef44b244764385e7b6bd681429b9b362` 的 82 个权重行逐项一致；新版的实质改进是输入路径可配置、数据新鲜度检查及执行账本保护。
- 用同一 QMT raw、公司行动和 ¥1,000,000 初始资金运行 81 个月账本：新版比旧版最终 NAV 高 ¥34,204.16（218.47bp）。差异来自对 `513100.SH` 5 倍、`513500.SH` 2 倍份额调整的正确处理，以及 stale-close 显式记录。
- QMT HFQ 与独立 Tushare 总回报/因子数据存在不可混用差异；模型数据源继续冻结为 QMT HFQ，独立源仅用于监控。
- 正式 target 增加真实月末保护；原 `4941029` 对象未交付，已由远端提交 `8ebfd21a159c74b73397ffb3847878a597d055df` 等价重建：只有冻结日历证明下一交易日已进入下一个自然月才允许发布。

研究端可签结论：**模型权重等价，且新版 raw 执行账本更完整，可进入 mock/paper 验证。**

研究端不可签结论：**真实现金流已核对。** QMT factor 响应没有分红实际支付日；真实资金前必须取得带支付日的正式公司行动来源，并接入 cash-flow journal。

详细审计见 `research_delivery/audit_20260821/RESEARCH_DELIVERY_STATUS_20260824.md`；8 月 31 日正式产物按 `research_delivery/HYDRA_20260831_RESEARCH_DELIVERY_RUNBOOK.md` 执行。

## 13. 截至今日的剩余清单

### 8 月 31 日研究端正式交付

- [ ] 收盘后以正式 `as_of_date` 冻结四份 QMT 包及各自 manifest/SHA-256；若发现遗漏，只创建修订包，绝不改写已交付包。
- [ ] 以冻结日历验证月末与次月首个交易日，生成正式 `Hydra_latest.parquet`、sidecar、输入 hash、publisher commit 和审计包。
- [ ] 运行 raw shadow ledger、QMT/独立源差异报告，并作出 `APPROVED_FOR_MOCK_PAPER`、`RESEARCH_ONLY` 或 `REJECTED` 的书面结论。

### Server 与 QMT client

- [x] 以可验证替代提交 `8ebfd21a159c74b73397ffb3847878a597d055df` fast-forward 研究候选分支并推送现有审计材料。
- [ ] 正式 publisher allowlist 仍待 8 月 31 日完整交付与书面批准。
- [x] 9 只 ETF 白名单双端固定校验，`511010.SH` 从 raw、target 与订单硬拒绝。
- [x] 实现 target / rebalance / attempt / order-trade 四层持久状态，QMT `trade_result` 为订单终态唯一依据。
- [x] 实现动态 `auto` 风控；执行前仍强制可用现金、可卖持仓与 50bp 保护限价三项硬约束。
- [ ] 完成私有 paper/live 配置与隔离：身份、token、账户指纹、userdata、session、SQLite、日志、任务名和目录均分开；私有信息不得提交 Git。
- [ ] 使用 Windows 任务计划程序部署已确认的 15:10、15:30、16:00、18:00、T+1 09:10 流程。

### 验收与进入实盘前置条件

- [ ] 连续两次相同 basket mock replay：结果 hash 相同，且没有真实 QMT 调用。
- [ ] 专用模拟账户完成完整月末/月初闭环，并演练部分成交、拒单、未决订单、收盘 residual、次日补单、急停及审计导出。
- [ ] 取得带实际支付日的公司行动来源，完成 cash-flow journal 与真实现金 P&L 对账。
- [ ] 书面批准后，才可用 ¥200,000 进入小规模实盘灰度；首次完整月度调仓后复盘成交质量、跟踪误差、现金流和对账，再决定是否增资。
