# Hydra 月度服务器上线回执 · 2026-09-18

## 已上线

- 生产代码：`879f94827bf199318590902734f4dcee6d015b42`。
- 原生产代码：`ed1dffd003d87821604b0577494ad06f8b4cfbb6`。
- 服务器目录：`/opt/qmt-server`，分支 `codex/hydra-monthly-server-20260918`。
- HTTP 服务于 **2026-09-18 12:12:10（上海时间）**进入运行状态；上线后独立复核 `active/running`，`NRestarts=0`。
- `/healthz=ok`，`/readyz=ready`，数据目录与持仓归属检查均为 `ok`。
- 运行中 OpenAPI 同时包含新增 `/hydra/research/snapshots` 与原有 `/hydra/execution/advance`。

本文件为上线后的文档回执；审阅分支可能比上述运行代码多出文档提交，不表示生产服务又切换了代码。

## 数据、资金与订单

停服切换前使用 SQLite backup API 备份并通过 `quick_check`。配置、数据库、逐表摘要和部署回执保存在服务器私有目录：

`/var/backups/qmt-server/monthly-20260918T041159Z`

原有 **25 张表**在服务重启后、研究 worker 首次运行后均逐表核对：行数与内容 SHA-256 **全部相同**。新增月度收件表为空。

没有重置账本，没有增减策略资金，没有改持仓，没有重做九月交易，没有创建新 target/交易订单，更未连接真实 QMT 发单。旧数据库不会作为普通代码回退手段覆盖新的交易事实。

## 研究运行环境

- 固定源码：`aa6b60deef44b244764385e7b6bd681429b9b362`。
- 路径：`/opt/qmt-refresh/releases/hydra-aa6b60d`；以 `qmtserver` 用户读取和运行。
- 进程：`qmt-hydra-monthly.service`，与 HTTP 分离，最多运行十二分钟，内存上限 2 GiB。
- 调度：`qmt-hydra-monthly.timer` 已启用，北京时间工作日 15:00—18:50 每十分钟检查。
- 首次人工启动仅检查收件箱，退出码 0，日志为 `IDLE_NO_NEW_MONTHLY_INPUT`、`order_count=0`。这不是月度生成失败：9 月 18 日不是本次约定的月末生成日。

## 验证证据

1. 本地服务器回归：681 passed、1 skipped；最终客户端回归：144 passed。
2. 服务器隔离工作区使用生产依赖完成全量回归：823 passed、1 skipped。随后最终时区修复提交再次执行全部客户端及月度状态机测试：155 passed。
3. 新研究程序读取生产保存的 20260903 四份真实冻结数据：9 个权重与历史审核结果最大绝对差 **0.0**，未接触交易写接口。
4. 实际 `qmtserver` 用户、实际生产 Python、安装后的固定研究目录再次完成同一重算并通过数值一致性检查。
5. systemd 日历与 service/timer 语法通过校验。环境原有 pytz 弃用提示和 snapd 单元未知字段提示不属于本次组件错误，未为此修改系统组件。

## 同伴还需要做什么

**只剩 Windows 数据采集端接入与真实环境验收，不是让 Windows 计算策略。** 本次没有切换 `C:\hydra-live`，没有修改 Windows 私有配置或计划任务。

1. 在独立干净工作区取得上述完整运行 commit，核对 SHA。不要将少数文件手工混入旧 release。
2. 用该版本 `v2.3/live_client/windows/Install-HydraLiveClient.ps1` 版本化安装；显式使用现有私有配置 `HYDRA_LIVE_PYTHON` 对应的 Python 路径。保留配置、状态库、旧 release 和安装备份。安装器会执行原生 PowerShell 解析与离线幂等验收。
3. 核对现有 15:30、16:00、18:00 任务确实指向安装器维护的版本化 runner/脚本；本次不改变时间表，不应重注册或重复增加另一套同名任务。
4. 非月末正常执行行情上传时，确认 `EXECUTION_DATA_PUBLISHED` 且 `monthly_research.status=NO_MONTHLY_RESEARCH_DUE`。不要为了验收而手工提交、撤单或改资金。
5. 首个月末确认 `RESEARCH_SNAPSHOT_STORED → MONTHLY_PLAN_READY → 晚间新计划订单`。如 18:00 研究尚未完成，应等完成后重跑晚间 `query-preflight`，不是重复运行早上的 submit。

没有 Windows/MiniQMT 原生验收回执之前，不能宣称整条月度自动链已经验收完成。服务器这边已就绪，旧交易执行链保持不变。

详细规则和故障处理见 [月度交接说明](HYDRA_MONTHLY_SERVER_HANDOFF_20260918.md)。
