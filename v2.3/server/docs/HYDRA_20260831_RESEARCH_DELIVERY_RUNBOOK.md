# Hydra 正式研究交付运行单：2026-08 月末

本运行单只生成研究、mock/paper 可消费的交付物；不得连接账户、查询账户状态或发出 QMT 委托。

## 前置条件

1. 代码固定为 `8ebfd21a159c74b73397ffb3847878a597d055df`，并记录实际运行的工作树提交。
2. 交易日历同时包含 `as_of_date` 和其后的首个交易日；首个交易日必须进入下一个自然月。否则禁止发布正式 target。
3. 模型 HFQ 包必须包含 9 个 executable ETF 与 `511010.SH` 的 `research_only` 历史桥接；raw 包仅含 9 个 executable ETF。
4. 运行前 SHA-256 校验本次四份输入包；运行后绝不改写同一包。发现遗漏时只能创建 `-r1` 新修订包。

## 月末收盘后步骤

1. 以当日收盘为 `as_of_date` 冻结四份 QMT 数据流：`hydra_model_hfq`、`hydra_execution_raw`、`hydra_corporate_actions`、`hydra_trading_calendar`。
2. 写入每份 package 的 row count、代码集合、最小/最大日期、SHA-256、QMT 来源、producer commit 和拉取时间；再生成一个总 manifest 与 ZIP 的 SHA-256。
3. 核验 `511010.SH` 仅出现在 HFQ 的 `research_only_symbols`，不在 raw、allowlist、target 或订单文件中。
4. 用 frozen HFQ 运行 Hydra 4.8，并以日历验证过的次月首个交易日作为 `--next-trading-date`。若不是完整月末，生成 smoke 报告即可，不生成正式 target。
5. 生成 `Hydra_latest.parquet` 与 sidecar JSON。sidecar 至少包含：`as_of_date`、`decision_date`、`execution_date`、strategy version、完整 publisher commit、9 个 executable codes、权重和、输入文件 SHA-256、target SHA-256、模型状态与信号日期。
6. 以 frozen raw 与公司行动执行 shadow ledger；输出 NAV、targets、orders、fills、holdings、cash、rebalance log、corporate actions、valuation log，以及月度汇总。
7. 生成 QMT-vs-independent 差异报告；raw 仅比较价格字段，HFQ 仅作为收益差异监测。任何公司行动因子异常须标记，不能用独立源覆盖 QMT HFQ。
8. 写出书面结论：`APPROVED_FOR_MOCK_PAPER`、`RESEARCH_ONLY` 或 `REJECTED`，并列明理由。

## 允许发布的最低条件

- target 恰为 9 个 executable ETF，权重非负且和为 1；
- `511010.SH` 不出现于 target；
- 月末/次月首日校验通过；
- 四份输入的 hash、target hash、sidecar hash、ZIP hash 全部匹配；
- 账本没有未解释的缺失价格、公司行动或重复事件；
- 若现金分红没有正式支付日数据，结论最多为 `APPROVED_FOR_MOCK_PAPER`，不得作为真实现金 P&L 已核对的证明。

## 本周 smoke 与正式月末的边界

`2026-08-21` 数据只能做 smoke/research：它不是月末，不能生成可发布的正式 target。正式包应使用 8 月最后交易日的收盘数据，并由冻结日历证明其下一交易日进入 9 月。
