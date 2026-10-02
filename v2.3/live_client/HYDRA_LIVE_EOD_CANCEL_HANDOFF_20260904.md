# Hydra 日终撤单与结案时序部署交接（2026-09-04）

## 目的

券商已确认：普通竞价委托必须在 15:00 前发出撤单，正式撤单终态约在 16:00
回报。旧任务在 15:10 尝试结案、16:00 请求 residual，既没有主动撤单，也早于
柜台终态，因此会让 attempt 长时间停在 `PENDING`。

本次只改变 Windows live-client 的订单生命周期，不改变 Hydra 选股、目标权重、
限价、资金子账本或 Server residual 算法。部署代码不会自动处理 2026-09-04 已经
错过撤单时点的三笔委托；它们仍须以 QMT/券商最终回报为准。

交付分支：

```text
codex/hydra-eod-cancel-lifecycle-20260904
```

部署时使用复审消息给出的完整 40 位 SHA，不使用短 SHA，不从工作区散拷文件。

## 新任务链

| 时间 | 任务 | 成功条件 |
|---|---|---|
| 14:55 | `Hydra-Live-CancelOpen-1455` | `CANCEL_REQUESTED`、`NO_ACTIVE_ORDERS` 或 `NO_ORDERS` |
| 15:30 | `Hydra-Live-MarketBackup-1530` | `UPLOADED` 或休市日 `SKIPPED_NON_TRADING` |
| 16:05 | `Hydra-Live-SettleClose-1605` | `ATTEMPT_CLOSED` 或 `NO_ORDERS`；进程失败时 16:10 最多重试一次 |
| 16:20 | `Hydra-Live-Retry-1620` | `RETRY_STAGED`、`NO_RESIDUAL`、`NO_ATTEMPT` 或 `ALREADY_STAGED` |
| 18:00 | `Hydra-Live-QueryPreflight-1800` | 下一交易日批次冻结、preflight READY、一次性 09:10 任务注册成功 |

14:55 撤单只读取本地冻结批次和 MiniQMT，不访问 Server。它只处理本地 state DB
中 `SUBMITTED` 的订单；先对完整候选集核对账户、代码、数量、限价、方向、
`strategy_name=hydra_live` 和确定性 remark，全部匹配后才逐笔发送撤单。不会查询或
撤销账户中其他策略、人工作业或 canary 的订单。

QMT 撤单接口返回 0 只记为 `REQUESTED`。`已报待撤(51)`、`部成待撤(52)` 均不是
终态；只有明确的已撤、部撤、已成或废单才能在 16:05 进入结案。完成 QMT 查询的
cancel-open 会在私有 evidence 目录保存带 SHA-256 的结果；任何订单请求失败会返回
`CANCEL_INCOMPLETE` 并报警。

## 部署步骤

先确认没有任务正在运行，导出旧任务 XML，并把交易开关暂时关闭。不要在 09:10、
14:55 或 16:05 窗口升级。

```powershell
$HydraDeployCommit = '<完整40位SHA>'
Set-Location C:\src\MaolaoyeServer
git fetch origin --prune
git cat-file -e "$HydraDeployCommit`^{commit}"
if ((git status --porcelain --untracked-files=no)) { throw 'Tracked worktree is dirty' }
git switch --detach $HydraDeployCommit
if ((git rev-parse HEAD).Trim() -ne $HydraDeployCommit) { throw 'Wrong commit' }

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\v2.3\live_client\windows\Install-HydraLiveClient.ps1 `
  -SourceRoot C:\src\MaolaoyeServer\v2.3 `
  -InstallRoot C:\hydra-live `
  -PythonExe <MiniQMT Python绝对路径> `
  -SourceCommit $HydraDeployCommit

C:\hydra-live\bin\Run-HydraLive.ps1 -Command doctor
```

安装器不会连接真实 Server/QMT、不会下单、不会修改任务。确认 installer 返回
`INSTALLED`、offline acceptance 为 `PASS`、doctor 显示
`server_contacted=false/qmt_contacted=false`。

先不带替换参数运行注册器。它应当因为发现旧的 15:10/16:00 任务而停止且不修改
现网：

```powershell
& C:\hydra-live\scripts\Register-HydraLiveOperationsTasks.ps1
```

人工核对旧任务名称后才执行：

```powershell
& C:\hydra-live\scripts\Register-HydraLiveOperationsTasks.ps1 `
  -ReplaceLegacyTasks
```

随后确认五个新任务均启用，旧
`Hydra-Live-SettleClose-1510`、`Hydra-Live-Settle-1510`、
`Hydra-Live-Trigger-1600`、`Hydra-Live-Retry-1600` 和
`Hydra-Live-Query-1800` 均禁用。不要手工创建每日固定 09:10 submit。

本提交没有 Server schema/API 改动。为保持仓库来源一致，可以在 Server 工作树 clean
且无部署任务运行时把 Git 指针推进到同一 SHA；核对 `git diff 1daf3334..<SHA> --
v2.3/server` 必须为空，因此不需要数据库迁移。无论是否更新指针，都要记录 Server
SHA、client active-release SHA 和上述 server diff 证据。

## 首日验收与停止条件

首日 14:55 保存 cancel receipt；确认只出现该批次的 Hydra order ID。16:05 必须看到
明确终态后才能 close；16:20 的 retry 订单内容仍只能由 Server 生成。18:00 保存
query/preflight 和一次性任务回执。

出现下列任一情况立即停止自动链路并报警：14:55 任务晚于 14:57 启动；本地有
`PREPARED`/`SUBMITTING_UNKNOWN`；QMT 身份字段不匹配；活动订单不在
`cancelable_only` 查询中；撤单请求返回非零；16:05 仍为已报、已报待撤、部成或
部成待撤；Server/QMT 对账不一致。绝不能为了继续流程而手工把 Server 订单改成已撤。

回滚时先关闭交易开关并禁用新任务。不要重新启用已知存在时序缺陷的旧 15:10/16:00
自动任务；恢复旧 client release 仅用于只读诊断，待问题处理后再重新切换。
