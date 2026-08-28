# Hydra P0 交付日志（2026-08-26）

## 交付范围

本次交付是研究端数据冻结能力的 P0 补齐，仅支持 research、mock 或
paper 消费；不连接、不查询真实 QMT 账户，也不发出委托。

## 代码基线与增量

- Server 上游基线：`62b3e840e5508a34db950995f356d438d3518291`。
- 本交付分支：`codex/month-end-p0`。
- P0 增量：`d74723cc1c0251e3f4423fcbd53ab992307211b3`、
  `6e23a7ab63cc590210436c947c795755ca3bdc4d`。
- Hydra 研究基线：`8ebfd21a159c74b73397ffb3847878a597d055df`；对应
  审计证据增量在 Hydra 仓库的 `a979b2cb6caff2cbfba7de358d43bffe68c1ff1f`。

## 已交付能力

1. 冻结器仅接收 QMT `userdata` 行情路径；不读取账户号、API Key、session
   或 live-client 私有配置。
2. 同一运行生成 HFQ、raw、公司行动、交易日历四包，逐包 manifest，聚合 ZIP
   和外部 SHA-256 收据；已有输出一律拒绝覆盖。
3. HFQ 固定包含 9 个 executable ETF 与 research-only `511010.SH`；raw 固定
   只含 9 个 executable ETF。
4. 公司行动与行情在同次 QMT 只读冻结中取得；不再复用旧日期的公司行动输入。

## 2026-08-26 冒烟冻结证据（不入 Git）

- `as_of_date`：`20260826`；仅作当前 smoke，不是月末正式包。
- 交付文件：`HYDRA_QMT_SNAPSHOT_20260826.zip` 及同名 `.manifest.json`。
- ZIP SHA-256：`8f5593a6ea0c9c3baaaa75830ced73633b9b11b0597cf538bf8c84e79fed3dde`。
- HFQ：28,335 行 / 10 个代码；raw：25,072 行 / 9 个代码；公司行动：20 行。

数据 ZIP、Parquet、QMT `userdata`、账户资料、token、session、SQLite 和日志均
不得提交 Git；应走受控文件交付，并以此 SHA-256 验收。

## 未放行事项

本交付不生成正式 target，也不构成 mock/paper 或真实交易批准。8 月 31 日收盘后
仍须按正式运行单重新冻结四包、验证跨月交易日、生成 target/sidecar、shadow ledger
和书面结论。

小资金实盘的三层账本、成交偏差、整手残余、风险贡献和相关性监控口径，见
`HYDRA_SMALL_CAPITAL_MONITORING_SPEC.md`。
