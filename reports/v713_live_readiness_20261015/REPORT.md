# V713 工程验证报告

状态：**本侧工程实现、合成联调和回归已验证，等待目标 Windows / QMT 现场验收。** 方案为 Hydra / V713 共用物理账户、各自 attributed 账本、单台账户执行器。本报告的测试数不是策略收益回测结果；已有 V713 研究证据及仍需完成的历史重放见 [回测与验证证据报告](BACKTEST_AND_VALIDATION.md)。

已测功能基线：`bcdfd166faf13281c535d514c73d809570231ac9`，基于 2026-10-08 的 `11bebfbc4782512c7037ddedd49ae3b584fd127b`。GitHub 候选分支：`codex/v713-live-readiness-20261015`，[草稿 PR #3](https://github.com/FionaMMC/MaolaoyeServer/pull/3)。后续文档 / 证据更新不改变该功能基线；接手流程见 [Handoff](../../docs/V713_SHARED_OMS_HANDOFF_20261015.md)。

## 已完成的工程

- 修复收盘不可交易状态被用于过滤次日买单的机制；保留真实状态及缺失值；收盘空盘口可读取当日缓存不复权价格，拒绝把早盘 tick 当收盘锚。
- 增加 V713 篮子发布适配、来源与月份幂等、原持仓零目标退出、显式股票/ETF 规格及冻结执行 policy。
- 增加账户级多策略计划及执行、策略现金/持仓/T+1 可卖量、跨策略预留、UNKNOWN 持久恢复、哈希/账户绑定和短期执行租约。自动和人工单共用风控，最新核对失败即不给执行许可。
- 使用券商观察到的冻结现金；共享未结买单缺该字段时阻断。费用按冻结证券规格投影，并提供实付费用更正、原子资金划拨、不可变期初、分红与公司行动复核回执、不复权估值证据。
- 操作台显式选择策略，支持单策略暂停；安装器备份实际双库、spool、HOLD 和绑定，任务支持停用登记。提供无需 pytest 的便携离线 gate、Windows 原生 parser 与 QMT 只读检查脚本。

详细逐项范围及尚需目标环境验收的 T00–T15 条目见 [主交接文档](../../docs/V713_SHARED_OMS_HANDOFF_20261015.md)。

## 验证结果

| 验证 | 结果 | 证据 |
|---|---|---|
| 本地服务端全套 | 964 通过，1 跳过，0 失败 | `server_tests.xml` |
| 本地客户端全套 | 266 通过，0 失败 | `client_tests.xml` |
| 干净源码包展开后的客户端全套 | 266 通过 | 打包回归；修复旧 collector 依赖遗漏后重跑 |
| qmt 现有 Python / 服务依赖上的服务端全套 | 964 通过，1 跳过，0 失败 | `remote_server_tests.xml` |
| 最终包在 qmt 上的客户端全套 | 266 通过，0 失败 | `remote_client_tests.xml` |
| 最终包真实 FastAPI + OmsAgent + 合成券商及独立安全回归 | 11 通过 | `remote_integration_safety_tests.xml` |
| 完整卖/次日买/余量窗口、共持 ETF、响应丢失/重启 | 通过，两场景 | `rehearsal_receipt.json`、`remote_rehearsal_receipt.json` |
| 便携 OMS 安装 gate | 本地、qmt 均通过 | 无真实 QMT / 服务端连接；UNKNOWN 预留、重启和过期租约 |
| 实际生产库的远程只读备份兼容性 | 42 个映射表列可读；原 schema / 行数不变；quick_check 正常 | `remote_schema_copy_receipt.json` |
| 操作台浏览器 | 显式选择、策略暂停/恢复、Hydra 不受影响、断开隐藏控制通过 | `browser_acceptance.json`；1440 / 390 宽度截图本地保留，无溢出、无 page error |
| 最终包校验 | 393 个源码文件、68 个 Windows ZIP 文件校验通过 | `package_verification.json` |
| 既有未跟踪文件保留 | 94 个 SHA256 全部未变 | `preservation_receipt.json` |

唯一跳过项为既有 V53 真实 bundle 端到端测试，原因是私有 `etf_close.parquet` 不在源码包/本地。未下载数据来绕过；V713 与共享 OMS 测试均实际运行。远端 pytest-asyncio、Starlette 有依赖弃用提示，不影响本次结果。

远端隔离目录：`/opt/qmt-server/private/research-runs/v713-live-readiness-20261015-0pvund_4`。现有运行时为 Python 3.11.15、FastAPI 0.136.1、Pydantic 2.13.3、SQLAlchemy 2.0.49；复用已有隔离 test-deps，仅给本次新目录添加 requests 2.32.3 及其依赖，不修改生产 venv。

远端最初因缺 requests 无法收集测试，补入隔离目录后服务端全量通过。随后干净包暴露一项旧 `client.market_push` 测试依赖遗漏，已修复打包清单，并在干净展开目录及 qmt 上各重跑客户端全套。最终源码与服务端全量通过时的**全部服务端文件完全相同**；变化仅为 Windows 检查/文档、打包工具及两份旧 collector 依赖。最终又运行 11 项联调/安全回归，详情及变更列表见 `remote_verification.json`。

生产数据库只以 `mode=ro + query_only` 创建远程一致性副本，新 schema 仅应用于副本。新增对象是 `accounting_receipts` 表及两个索引。原始账户、订单、行情和副本未回传本地。生产服务、生产配置、策略资金、目标、审批及交易开关均未改变；没有提交任何真实委托。

## 交给对方的材料与下一步

1. [主 Handoff](../../docs/V713_SHARED_OMS_HANDOFF_20261015.md)：GitHub 固定提交、改动范围、N01–N05 的负责人 / 动作 / 产物 / 通过条件。
2. [回测与验证证据报告](BACKTEST_AND_VALIDATION.md)：V713 已有研究和短期模拟账本、Hydra 执行研究、本轮工程测试的分别结论，以及我方仍需补做的历史回放。
3. 本工程验证报告及同目录 JSON / JUnit 回执。

对方首先按 [Windows / QMT 验收步骤](../../docs/V713_SHARED_OMS_WINDOWS_ACCEPTANCE_20261015.md)，用 GitHub 的固定功能提交和现有安装脚本完成现场取证、离线与只读验收，回传 `before`、`local`、`qmt-readonly` 约定的脱敏文件。开发侧负责核对、修复兼容性并复测；再根据已确认的证券 / 费用 / policy / 资金输入完成 V713 历史重放和首轮 dry-run。通过后才形成具体生产启用步骤。详细顺序和交接条件以主 Handoff 的 N01–N05 表为准。

## 可选归档附件

此前生成的 `artifacts/v713-shared-oms-windows.zip`、`v713-readiness-source.tar.gz`、`RELEASE_MANIFEST.json`、`build_receipt.json` 是固定功能版本的离线源码快照（本地生成物，不重复放入 Git）。它们保留以下原校验记录；**GitHub / server 主流程不要求下载 ZIP**，后续交付文档从 PR 获取。包内不含生产 env、行情或账户库。

最终源码集合 SHA256：`e8c520e5b9da3957ca31927ade58ce0aefb40f2bb83d49c8c25fb965ac276eab`。

| 包 | SHA256 |
|---|---|
| `v713-readiness-source.tar.gz` | `89c8bf38f0aa0085aaae6d4c3d119bc8c91d68803aa0942d4226d5e114e57730` |
| `v713-shared-oms-windows.zip` | `d8ca652f162d47b36c47fca131e8ad01a8a1b7606d862177ae4959ddeb0d207d` |

## 目标环境尚需验收

- Windows PowerShell 5.1 原生解析/执行、实际 active release/任务入口/时区、双库/账户锁/重启恢复。开发机没有 PowerShell，静态与嵌入 Python 测试不冒充原生通过。
- 实际 QMT SDK 的 `frozen_cash`（需 observed=true）、PriceTick、当日价格边界、收盘 tick / 缓存日线来源和时间、账户板块权限、订单备注完整回读。默认只读验收不报单、不撤单。
- 报告三笔历史单的券商终局与旧缓存责任；本次未重放或改写事故周期。
- 经确认的 V713 policy、真实期初资金及来源、单一执行器、首轮目标与订单差异清单。上述输入形成具体方案后，再进入生产部署、启用与首轮真实成交/日终核账。

对方回传约定的脱敏验收文件后，开发侧按失败项修正并复核，继续完成规则冻结、历史重放和首轮计划。工程验证完成不代表研究与实盘准入已全部完成；无需重新做本轮工程实现或用旧回测收益替代这些结果。
