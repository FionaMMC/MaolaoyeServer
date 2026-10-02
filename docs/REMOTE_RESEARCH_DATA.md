# 远程研究数据

用户于2026-09-28要求后续查看、分析数据使用远程数据，本地不再保留原始行情副本。

主机：`ssh qmt`

## 生产已有数据

- 日线行情：`/opt/qmt-server/v2.3/server/data/market/daily/`
- Hydra原始行情、复权模型行情、公司行动、交易日历批次：`/opt/qmt-server/v2.3/server/data/hydra/batches/`
- 账户与订单数据库：`/opt/qmt-server/v2.3/server/pipeline-server.db`，分析时使用SQLite `mode=ro` 和 `PRAGMA query_only=ON`，仅返回必要汇总。

本次检查时，生产行情目录只有日线，没有本次新浪/东方财富分钟样本；原有生产目录不作更改。

## 本次研究归档

根目录：`/opt/qmt-server/private/research-data/etf-execution-20260928/`

归档保存114个文件（15,343,359字节），包含原始数据、回测依赖、脚本及当时的结果报告，目录结构保持不变：

|内容|相对归档根目录的路径|
|---|---|
|三只ETF新浪五分钟、东方财富一分钟样本|`reports/execution_experiment_20260928/public_minutes/`|
|ETF原始日线、公司行动、数据接口回包|`reports/execution_experiment_20260927/private/`|
|复权行情、重建目标权重、历史研究依赖与证据|`reports/hydra_performance_20260918/private/`|
|逐文件大小与SHA-256|`MANIFEST.json`|
|上传校验回执|`UPLOAD_RECEIPT.json`|
|服务器直接读取校验回执|`READ_VERIFICATION.json`|

本地索引：`reports/remote_data/etf_execution_20260928_manifest.json`。清理回执：`reports/remote_data/etf_execution_20260928_cleanup.json`。

## 后续使用

优先直接在服务器读取数据，使用已有Python环境：`/opt/qmt-server/venv/bin/python`。例如在远程重新核验分钟数据：

```sh
ssh qmt '/opt/qmt-server/venv/bin/python /opt/qmt-server/private/research-data/etf-execution-20260928/reports/execution_experiment_20260928/validate_minutes.py'
```

该脚本读写的是独立研究归档目录，不是生产数据目录。需要保留原归档结果时，先在服务器建立新的研究工作目录，再运行分析。读取市场数据只返回所需统计、覆盖范围与校验结果，不把原始行情重新传回本地。

本地报告及脚本仍可阅读；依赖原始行情的回测应使用服务器研究副本运行。本地原始数据目录、其空目录和对应Python缓存已清理，下载/上传临时包也会在远端校验完成后删除。

## 服务器直接下载与分时回测（2026-09-28）

新的工作副本：`/opt/qmt-server/private/research-runs/etf-intraday-20260928/`。

研究入口：该目录下的 `reports/execution_experiment_20260928/server_intraday.py`。此脚本仅允许在服务器研究工作目录运行；所有公开行情请求从服务器发出。

- 原始分时文件：`reports/execution_experiment_20260928/server_fetched_minutes/`
- 下载时间、来源、行数和SHA-256：`reports/execution_experiment_20260928/server_download_receipts.json`
- 回测汇总：`reports/execution_experiment_20260928/SERVER_INTRADAY_RESULT.json`
- 本地仅保存汇总：`reports/remote_data/server_intraday_20260928_summary.json`

本次直接从新浪成功取得九只ETF各1,970根五分钟线，以及黄金/纳指/标普各1,970根一分钟线，合计23,640根。东方财富一分钟接口在服务器断连，改用新浪备用接口；本次没有使用Tushare凭据。下载包含9月28日上午数据，回测排除了未收盘的当日。

五分钟线共同完整覆盖40个交易日；与旧研究日线共同可回放区间为8月3日至9月3日，完成两轮原策略目标、14笔模拟买卖需求的连续组合回测。起始持仓来自历史模型重建，不是实盘账户；结果是分钟撮合模型，不是券商真实成交。

后续新日期下载应创建新的研究工作副本，保留本次下载回执和原始样本。原归档`research-data/etf-execution-20260928`保持不变。

## 完整限价与期限对照（2026-09-28）

独立研究副本：`/opt/qmt-server/private/research-runs/etf-final-20260928/`。

本次入口及结果位于其 `reports/execution_final_20260928/`：

- `final_suite.py`：32种限价／期限配置，日线与5/15/30/60分钟回放、压力测试、1/5分钟交叉检查。
- `raw/` 与 `receipts.json`：新下载的九只ETF的15/30/60分钟原始数据，共53,190根，保存请求时间和SHA-256。已有1/5分钟原始数据仍在该工作副本的兄弟研究目录。
- `*_order_detail.csv`、`*_cycle_detail.csv`、`*_nav*.csv`：逐单、逐轮与净值明细仅保留远程。
- `daily_summary.json`、`intraday_summary.json`、`opportunity_summary.json`、`audit_summary.json`：可回传本地的汇总。
- `FINAL_DECISION.md`、`policy_comparison.csv`、`fund_comparison.csv`、`minute_stress.csv`：最终报告及对照表，本地同名路径在 `reports/execution_final_20260928/`。

60分钟主样本覆盖2024-10-08至2026-09-03，23轮调仓、三只ETF合计27笔历史买入需求。原始数据与生产配置未改动。报告区分真实策略历史需求的模拟、近期假设买单、日线与粗分时证据；完成度不等于实盘保证。

历史分钟最后成交价可能不同于日线官方收盘价。策略参考价与净值统一使用QMT日线收盘价，分钟撮合只使用棒的开、高、低与量，不把分钟末价作为官方收盘替代品。初轮更严格筛掉收盘差异日的结果保存在远程 `initial_strict_close_summary.json`。

## Hydra归因、收盘影子账及双阶段回放（2026-09-30）

本地汇总与入口：`reports/hydra_acceptance_20260930/REPORT.md`。归因只读生产数据库与行情，原始收益及账户明细没有回传。归因截至9/29，最终影子账历史检查截至各自最新快照（Base/Hydra到9/30）。

- 最终影子账隔离代码与结果：`/opt/qmt-server/private/research-runs/hydra-acceptance-20260930-8mppvqmo/`，`history_audit_summary.json` 保存汇总和测试代码哈希；使用内存测试账本，不修改生产数据库。
- 收盘卖/次日开盘买研究：`/opt/qmt-server/private/research-runs/hydra-acceptance-20260930-e_f43kjh/two-phase/`，`two_phase_summary.json`为汇总，`*_nav.csv`、`*_cycles.csv`、`*_fills.csv`为仅远程保留的明细。读取原有ETF冻结归档，没有下载新的行情。
- 研究覆盖82轮、12个情景；不是实盘部署或真实前向测试。旧影子账仍有缺失历史目标及估值/持仓转换差异，详见报告，不能将模拟成交价与真实成交价的差额抹掉。
