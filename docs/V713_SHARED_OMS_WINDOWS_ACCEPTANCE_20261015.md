# V713 Shared OMS：Windows / QMT 安装与只读验收

日期：2026-10-15。交付对象：维护 Windows、MiniQMT 和任务计划程序的人员。

本文件说明通过既有 **GitHub / server 交付流程**取得固定源码后，在 Windows 安装和只读验收的方法，不表示生产已部署或真实交易已通过验收。整个流程保持交易关闭、任务停用；不提交或撤销真实订单。服务端发布、策略资金分配、冻结 V713 policy、真实订单验收另行处理。

本次已测功能基线固定为 **`bcdfd166faf13281c535d514c73d809570231ac9`**。本说明可以由后续文档提交更新，但不能把文档所在提交或分支最新 HEAD 自动替换成安装的功能基线。ZIP 仅为可选离线附件，主流程不需要 ZIP 或 `RELEASE_MANIFEST.json`。

| 顺序 | Windows/QMT 人员做什么 | 产物 | 通过条件 | 下一步对接角色 |
| --- | --- | --- | --- | --- |
| 1 | fetch，创建固定 SHA 的独立干净 checkout | 源码 SHA、干净状态 | 与上述基线一致，原工作区未被覆盖 | 发布维护人员核对版本来源；通过后继续现场检查 |
| 2 | 采集 before，私有备份后停用调度 | `before` 报告、本机私有备份 | 实际入口可识别，任务停用且运行实例结束 | Windows 运维人员继续安装；账务/未知委托问题交 OMS 后端人员 |
| 3 | 保留账户绑定，关闭交易，执行安装器 | `INSTALLED` 结果、运行备份、active release | 双离线 gate/doctor 通过，任务未启用 | 客户端工程人员核对安装结果 |
| 4 | 运行不连 QMT 的本地验收 | `local` 目录五个 JSON | 原生 parser、离线 gate 通过，无 blocker | Windows/QMT 人员继续只读连接 |
| 5 | 显式连接 QMT，只读采样 | `qmt-readonly` 报告和行情 fixture | 账户指纹匹配，冻结资金字段已观测，所需原始字段可解释 | QMT 接口人员与客户端工程人员核对字段；策略人员核对标的规则 |
| 6 | 如需检查任务，使用 `-StageOnly` | `staged` 报告 | 九个 OMS 任务和旧任务均停用，入口路径正确 | Windows 运维人员回传结果 |
| 7 | 汇总脱敏证据与未覆盖项 | 第 8 节回传清单 | 不含私钥/账户明细，未执行真实交易 | OMS 后端与客户端工程人员完成双端结果核对 |

## 1. 从 GitHub 取得固定版本与执行环境

使用 **Windows PowerShell 5.1**，以维护该 QMT 实例和任务的同一 Windows 用户打开管理员窗口。使用当前已安装 `xtquant` 的 Python，不能随意换成系统 Python。无需 pytest。

从 Windows 上现有仓库 fetch，只更新 Git 对象和远程引用；不对原工作区执行 checkout、reset、clean 或覆盖复制。下列 `$ExistingRepo` 请填写本机现有仓库，`origin` 沿用它已配置的 GitHub 远程和认证方式：

```powershell
$ExistingRepo = "C:\src\MaolaoyeServer"
$SourceCommit = "bcdfd166faf13281c535d514c73d809570231ac9"
$SourceCheckout = "C:\handoff\v713-source-bcdfd166"
git -C $ExistingRepo status --short
if ($LASTEXITCODE -ne 0) { throw "请核对既有仓库路径" }
git -C $ExistingRepo fetch origin codex/v713-live-readiness-20261015
if ($LASTEXITCODE -ne 0) { throw "fetch 失败，保留现有现场并核对远程连接" }
git -C $ExistingRepo cat-file -e "$($SourceCommit)^{commit}"
if ($LASTEXITCODE -ne 0) { throw "未取得指定功能基线，请发布维护人员核对远程提交" }
if (Test-Path -LiteralPath $SourceCheckout) { throw "目标目录已存在；另选新路径，不删除或覆盖旧目录" }
git -C $ExistingRepo worktree add --detach $SourceCheckout $SourceCommit
if ($LASTEXITCODE -ne 0) { throw "独立 worktree 创建失败；核对 Git 支持情况或使用下述独立 clone" }
```

原工作区有未提交修改也可以使用独立 worktree，原修改留在原处。若本机 Git 不支持 worktree，使用**独立 clone 替代上面最后的 worktree 步骤**。两种方式选一种，不在失败后残留的目录继续 clone：

```powershell
$SourceCheckout = "C:\handoff\v713-clone-bcdfd166"
if (Test-Path -LiteralPath $SourceCheckout) { throw "请选择新的独立 clone 目录" }
$RemoteUrl = (git -C $ExistingRepo config --get remote.origin.url)
if ($LASTEXITCODE -ne 0 -or -not $RemoteUrl) { throw "无法读取既有 origin；请核对远程配置" }
git clone --no-checkout $RemoteUrl $SourceCheckout
if ($LASTEXITCODE -ne 0) { throw "独立 clone 失败" }
git -C $SourceCheckout checkout --detach $SourceCommit
if ($LASTEXITCODE -ne 0) { throw "无法 checkout 指定功能基线" }
```

确认独立源码的 HEAD 与干净状态，再设置安装参数。保留新版本操作说明在 checkout 之外，不修改这份固定源码：

```powershell
$ActualCommit = (git -C $SourceCheckout rev-parse HEAD)
if ($LASTEXITCODE -ne 0 -or $ActualCommit -cne $SourceCommit) { throw "源码 HEAD 与指定基线不符" }
$SourceChanges = @(git -C $SourceCheckout status --porcelain)
if ($LASTEXITCODE -ne 0 -or $SourceChanges.Count -ne 0) { throw "独立源码必须干净，不能夹带现场修改" }
$SourceRoot = Join-Path $SourceCheckout "v2.3"
$SourceWindows = Join-Path $SourceRoot "live_client\windows"
$InstallRoot = "C:\hydra-live"
$EnvFile = Join-Path $InstallRoot "config\hydra-live.env"
$PythonExe = "C:\private\miniqmt-python\python.exe"
$Evidence = Join-Path "C:\handoff" ("v713-acceptance-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw "请填写既有 QMT Python 的绝对路径" }
New-Item -ItemType Directory -Path $Evidence | Out-Null
$ActualCommit | Set-Content -LiteralPath (Join-Path $Evidence "source-commit.txt") -Encoding ASCII
"CLEAN" | Set-Content -LiteralPath (Join-Path $Evidence "source-worktree-status.txt") -Encoding ASCII
Get-TimeZone | Select-Object Id
$PSVersionTable.PSVersion
& $PythonExe --version
```

时区必须是 `China Standard Time`。若不符，停止任务注册，由主机维护人核对其他任务后处理时区；本流程不自动修改时区。当前注册脚本的 runner 固定为 `C:\hydra-live\scripts\Run-HydraOms.ps1`，因此本次必须使用上述安装目录，不能只改 `-InstallRoot` 后继续注册。

若本机执行策略阻止直接运行 `.ps1`，先保留该失败。主机维护人可按既有运维规范，在上述 SHA / 干净状态核对完成后，对**当前维护进程**使用 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force`；不修改 CurrentUser / LocalMachine，进程退出即失效。若组织 MachinePolicy / UserPolicy 仍限制执行，将错误交主机维护人处理，不尝试绕过组织策略。后续命令沿用同一窗口中的变量。

## 2. 先取证，再停用旧调度

已有安装时，先从上述固定版本 checkout 运行新检查脚本，读取当前 active release；默认不连接 QMT、服务器，不改配置、journal 或任务：

```powershell
& (Join-Path $SourceWindows "Inspect-V713SharedOms.ps1") `
  -OutputDirectory (Join-Path $Evidence "before") `
  -InstallRoot $InstallRoot -EnvFile $EnvFile -PythonExe $PythonExe
```

它需要已有 `config\active-release.txt`、对应 release 和 `release.json`。如果是首次安装，这三项不存在，可跳过安装前检查，安装后再检查；如果是升级却缺失或哈希不符，保留错误，先定位实际运行入口，不伪造 manifest 或删除文件绕过检查。

如果旧版本有临时改动或清单不一致，检查器可能在生成三个 `before` JSON 前退出。此时允许回传“失败阶段及错误类型、上述源提交、`active-release.txt` 的版本值、实际 `gateway.py` / runner 的路径和 `Get-FileHash -Algorithm SHA256` 摘要”，注明 JSON 未生成；开发侧据此核对差异，不要求先修清单或覆盖代码凑齐回执。只传路径 / 版本 / 哈希，不传私有 env。

取得维护窗口后，导出当前任务定义和原配置到**本机私有备份目录**。XML、私有 env 不在回传清单内。不要把私有备份放入 `$Evidence`：

```powershell
$PrivateBefore = Join-Path (Join-Path $InstallRoot "backups") ("before-v713-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $PrivateBefore | Out-Null
Copy-Item -LiteralPath $EnvFile -Destination (Join-Path $PrivateBefore "hydra-live.before.env")
$HydraTasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
    $_.TaskName -like "Hydra-Live-*" -or $_.TaskName -like "Hydra-Oms-*"
})
$taskIndex = 0
foreach ($task in $HydraTasks) {
    $taskIndex += 1
    Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath |
      Set-Content -LiteralPath (Join-Path $PrivateBefore ("task-{0:D3}.xml" -f $taskIndex)) -Encoding UTF8
}
$HydraTasks | Disable-ScheduledTask | Out-Null
Get-ScheduledTask | Where-Object {
    $_.TaskName -like "Hydra-Live-*" -or $_.TaskName -like "Hydra-Oms-*"
} | Select-Object TaskName, State
```

停用触发器后，确认已有运行实例自然结束、当前无手工客户端进程持有账户。不要强杀正在报单的进程，也不要清理锁文件来绕过账户锁。安装器要求所有相关任务为 `Disabled`；如仍有 `Running` 或无法确认委托终局，停止升级并保留现场。

## 3. 私有配置与账户绑定

在本机编辑既有 `$EnvFile`。升级时保留原账户、账户别名、execution domain、task prefix、state 路径、QMT userdata/session、API 凭据、服务器地址和隔离配置，不把示例整份覆盖上去。本阶段只切换共享 OMS profile，并明确关闭真实交易：

```dotenv
HYDRA_LIVE_MODE=live
HYDRA_LIVE_EXECUTION_DOMAIN=live
HYDRA_LIVE_EXECUTION_PROFILE=shared_oms
HYDRA_LIVE_TRADING_ENABLED=false
HYDRA_LIVE_CANARY_ENABLED=false
HYDRA_LIVE_RISK_MODE=disabled
```

以下是**结构示例**，尖括号内容不可原样执行，也不得回传真实值：

```dotenv
HYDRA_LIVE_QMT_ACCOUNT_ID=<保留本机账户号>
HYDRA_LIVE_EXPECTED_ACCOUNT_SHA256=<该账户号UTF-8字节的SHA256，64位小写十六进制>
HYDRA_LIVE_ACCOUNT_ALIAS=<保留服务端已绑定别名>
HYDRA_LIVE_INSTANCE_ID=<保留既有Hydra实例，不能改为V713来代替账户共享协议>
HYDRA_LIVE_TASK_PREFIX=<保留原client_id对应前缀>
HYDRA_LIVE_QMT_USERDATA_DIR=<既有实盘userdata目录绝对路径>
HYDRA_LIVE_QMT_SESSION_ID=<既有正整数session，不能与paper客户端冲突>
HYDRA_LIVE_STATE_DB=<既有state数据库绝对路径>
HYDRA_LIVE_LOG_DIR=<既有日志目录绝对路径>
HYDRA_LIVE_PYTHON=<安装xtquant的python.exe绝对路径>
HYDRA_LIVE_API_KEY=<保留私有凭据>
HYDRA_LIVE_SERVER_BASE_URL=<保留获准的服务端地址>
HYDRA_LIVE_LEDGER_MODE=attributed
HYDRA_LIVE_INITIAL_ALLOCATED_CASH=<保留已核验的初始值，不能用账户余额重置>
HYDRA_LIVE_INITIAL_ALLOCATED_POSITIONS_JSON=<保留既有Hydra配置>
HYDRA_LIVE_ALLOWED_SYMBOLS=510300.SH,159915.SZ,511260.SH,518880.SH,159981.SZ,159985.SZ,159930.SZ,513500.SH,513100.SH
```

其他必需字段以固定 checkout 内 `v2.3\live_client\.env.example` 和既有配置为准。`HYDRA_PAPER_QMT_ACCOUNT_IDS`、`HYDRA_PAPER_QMT_SESSION_IDS`、`HYDRA_PAPER_WRITABLE_PATHS`、`HYDRA_PAPER_TASK_PREFIXES` 同机隔离信息必须保留；不能为了通过校验而填假值或关闭隔离。HTTP 设置沿用已批准配置，不在本次验收中放宽。

共享账户仍只有一套调度和一个 `oms-agent.db`；V713 股票权限来自服务端签发的 v2 plan，不通过把股票追加到旧的九只 ETF 白名单获得。V713 的资金归属和初始持仓由服务端单独确认，此处不初始化或重置它们。

`oms-agent.db` 位于 `HYDRA_LIVE_STATE_DB` **同一目录**。绑定由 `account_alias`、`execution_domain`、账户指纹、`client_id=HYDRA_LIVE_TASK_PREFIX` 四项构成。检查结果只输出指纹/绑定摘要和是否匹配，不输出原始账户号或私有 env。绑定不符应停止；不要删除或改写 journal 以强行绑定。首次尚无 journal 时，检查不会创建它；`present=false` 不等于正式运行的绑定已验收。

已有 journal 若 `present=true` 但 `binding_present=false`，属于旧版尚无绑定证据，和“存在绑定但值不匹配”分开记录；检查器仍会保守输出 `JOURNAL_BINDING_UNVERIFIED`。安装器 / doctor 不会替 OMS journal 生成该绑定，因此此分支先停留在 Handoff N03：回传证据，由开发侧核对旧订单、缓存和账户身份，准备有据的绑定迁移与复测步骤。不要手改 SQLite、删除 journal 或运行交易 agent 来消除 blocker；该项是否存在，以现场检查结果为准。

## 4. 安装：保留运行证据，不启用任务

调用固定 checkout 中原有的 `Install-HydraLiveClient.ps1`；`-SourceRoot` 指向包含 `live_client` 的 `v2.3` 目录，不是仓库根、`live_client` 或 `windows` 目录。`-SourceCommit` 必须是上述固定完整 SHA；安装器会检查源码工作区干净，并自行生成安装目录内的 `release.json`，不要求事先提供 `RELEASE_MANIFEST.json`：

```powershell
& (Join-Path $SourceWindows "Install-HydraLiveClient.ps1") `
  -SourceRoot $SourceRoot -InstallRoot $InstallRoot `
  -EnvFile $EnvFile -PythonExe $PythonExe -SourceCommit $SourceCommit
```

安装器依次进行 PowerShell/Python 语法检查、原离线 gate、`python -m live_client.oms_offline_acceptance`；随后持有账户字节锁，备份后切换代码并运行本地 doctor。doctor 会初始化/迁移旧 state schema，所以**安装本身是本地写操作**；它不连接服务器或 QMT，不代表后续只读验收已完成。

应看到最终 `status=INSTALLED`、`offline_acceptance=PASS`、`oms_offline_acceptance=PASS`、`local_doctor=PASS`、`tasks_modified=false`。保留非敏感结果摘要。实际布局：

| 内容 | 路径 |
| --- | --- |
| 不可变代码包 | `C:\hydra-live\releases\<完整source_commit>\live_client` |
| 包清单 | `C:\hydra-live\releases\<完整source_commit>\release.json` |
| 当前版本指针 | `C:\hydra-live\config\active-release.txt` |
| 兼容旧运行入口 | `C:\hydra-live\bin\Run-HydraLive.ps1` |
| OMS 运行、检查、验收、任务脚本 | `C:\hydra-live\scripts\` |
| 本次私有运行备份 | 最终输出 `backup_root`，即 `C:\hydra-live\backups\<时间戳>` |

备份包含 `hydra-live-state-before-upgrade.db`、`oms-agent-before-upgrade.db`（原文件存在时），使用 SQLite backup API 并执行 `quick_check`，不是直接复制正在使用的 DB 文件；还包含 `oms-runtime\oms-spool\`、全局/策略 `HOLD`、`HOLD-*`、`HOLD.*`、旧代码指针/脚本、env 和 `runtime-backup.json` 校验清单。备份目录留在本机，不整体回传。

## 5. 不连接 QMT 的本地验收

安装后改用已安装的脚本，每次使用新的输出目录：

```powershell
& "C:\hydra-live\scripts\Run-V713OmsAcceptance.ps1" `
  -OutputDirectory (Join-Path $Evidence "local") `
  -InstallRoot $InstallRoot -EnvFile $EnvFile -PythonExe $PythonExe
```

此命令验证 active release/运行脚本哈希，运行目标 Windows 的原生 PowerShell parser、可移植 OMS 离线 gate，并只读检查 journal、任务和版本。默认不连接 QMT或服务器，不修改配置、任务或 journal；离线 gate 的临时模拟文件使用临时目录。

`local\acceptance.json` 期望 `status=PASSED_LOCAL_ACCEPTANCE`、`offline_gate=PASS`、`native_powershell_parser=PASS`、`qmt_connection_check=NOT_REQUESTED`，同时 `broker_submission_acceptance=NOT_PERFORMED`。这只是本地验收通过。检查 `local\shared-oms-inspection.json`：

- `blockers` 应为空，包和已安装 runtime hash 匹配，Windows 时区正确。
- 已有 journal 的 `integrity_ok`、`binding_matches` 应为 `true`；`unknown_count`、`submitting_count` 应为 0。
- 若为前述 legacy 无绑定，保持 `BLOCKED` 回传开发侧，不把它当错账户，也不要求本地安装自动修复；待有据迁移及复测通过后再继续。
- HOLD、未送达 outbox/spool 必须解释。UNKNOWN/SUBMITTING/HOLD 是保留证据的理由，不是待删除的垃圾。
- QMT 进程版本与 `xtquant_version` 是不同指标；无法读取时可能为 `null`。补充 QMT“关于”和 `xtquant` 安装来源/版本的脱敏截图或文字，不把 `null` 当作已验证。

失败时保留输出，不重复使用同一个目录，也不修改包清单、删除 HOLD 或清空 journal 来取得 PASS。

## 6. 必需的真实 QMT 只读连接与元数据采样

通过本地检查、MiniQMT 已登录既有实盘账户后，做一次显式只读连接。保持所有任务 `Disabled`、交易开关 `false`、风控 `disabled`。此入口只调用账户快照/当日委托查询、原始 tick 和 instrument detail，不调用报单、撤单、agent 周期或服务端写接口。

先选当前待验收 V713 篮子及退出持仓涉及的代表性股票，另选一个既有 ETF 对照；每次最多 20 个代码。下面两只代码仅演示查询格式，执行前应换成待验收清单，不构成交易指令：

```powershell
$FixtureSymbols = @("000001.SZ", "510300.SH")
& "C:\hydra-live\scripts\Run-V713OmsAcceptance.ps1" `
  -OutputDirectory (Join-Path $Evidence "qmt-readonly") `
  -InstallRoot $InstallRoot -EnvFile $EnvFile -PythonExe $PythonExe `
  -ConnectQmt -Symbols $FixtureSymbols
```

`-ConnectQmt` 会取得共享账户锁，并可能创建对应锁文件；不会写配置或 journal。它要求本地账户绑定等检查没有 blocker。连接输出只保留账户指纹、持仓品种数量、当日委托数量和冻结资金字段是否可观测的布尔标记，不导出账户号、金额、持仓明细或订单明细。

核对 `local-runtime.json` 的 `qmt_query.frozen_cash_observed` 必须为 `true`。它表明券商账户快照实际提供了 `frozen_cash` 字段；真实数值为零也属于已观测，但缺失不能填成 `0` 或从委托列表估算。字段缺失时报告 `FROZEN_CASH_UNOBSERVED` blocker，验收结果为 `BLOCKED`，应由券商/QMT接口侧补齐证据后重验。

`qmt-readonly\acceptance.json` 应显示 `qmt_connection_check=PASS`、`qmt_contacted=true`，但真实报单仍为 `NOT_PERFORMED`。必须回传 `qmt-quote-fixtures.json` 原始字段，供工程侧确认当前券商/QMT 的实际语义：

| 需要确认 | 采样内容/处理 |
| --- | --- |
| 价格网格及当日价格边界 | instrument 的 `PriceTick`、`UpStopPrice`、`DownStopPrice`；必须为该标的当天有效值 |
| 交易状态 | instrument `IsTrading`，tick `openInt` 等原始字段；缺失不能擅自填 `true`，休市时为 false 也不能直接解释为次日停牌 |
| 时点与价格来源 | tick `time`/`timetag`、`lastPrice`、买卖档及采集时间；标明交易日、北京时间、连续竞价/集合竞价/收盘后所处阶段 |
| 股数/结算/费用规则 | 对实际可能交易的板块提供券商确认的买入最小量、增量、卖出整手/零股规则、T+1、佣金最低费/费率及卖出税费；不能从 ETF 规则或 tick 数据猜测 |
| 特殊场景 | 仅在真实可取得时补充停牌、涨跌停或特殊板块样本，并注明场景；缺失样本明确列为待验收，不制造报价 |

如需不同阶段的样本，用新目录重新执行同一个只读命令。一次成功连接或收盘后样本不等于 09:15/14:57 等实际执行阶段已验证；报单权限、拒单/部分成交/撤单、交易日历和费用落账仍需后续批准的独立验收，本文件不安排这些操作。

## 7. 只注册停用任务，检查最终入口

本地验收后可进行 `StageOnly` 注册。它会创建/替换九个 OMS 任务但保持 `Disabled`，不会启用任何任务，也不会停用/恢复旧任务。若只做完全只读检查、不允许任务变更，可跳过本节并在回传说明中标记“未注册”。

```powershell
& "C:\hydra-live\scripts\Register-HydraOmsTasks.ps1" `
  -InspectionDirectory (Join-Path $Evidence "local") -StageOnly

& "C:\hydra-live\scripts\Inspect-V713SharedOms.ps1" `
  -OutputDirectory (Join-Path $Evidence "staged") `
  -InstallRoot $InstallRoot -EnvFile $EnvFile -PythonExe $PythonExe
```

注册器要求检查目录内存在 `tasks.json`，即新检查/验收脚本生成的脱敏任务清单。核对 `staged\tasks.json` 中 `Hydra-Oms-Pre-0900`、`Buy-0914`、`Pre-1445`、`Cancel-1455`、`Sell-1456`、`Eod-1505`、`Eod-1530`、`Upload-1800`、`Intraday` 共九项均停用，动作指向 `C:\hydra-live\scripts\Run-HydraOms.ps1`。旧 `Hydra-Live-*` 和可能遗留的 `Hydra-Oms-Manual` 也保持停用。

本次不要去掉 `-StageOnly`，不要使用 `-DisableLegacyTasks` 默认启用路径，不要运行 `Enable-ScheduledTask`、手工触发 `Run-HydraOms.ps1` 的 pre/buy/sell/cancel/eod/intraday/upload-spool，也不要用旧 canary 流程代替只读连接。`TRADING_ENABLED=false` 不能代替停用全部调度，撤单和人工队列等路径需单独控制。

## 8. 回传清单与通过范围

回传 `source-commit.txt`、`source-worktree-status.txt`，以及执行时间/Windows 与 Python 版本、安装结果非敏感摘要。检查报告中应能看到相同的 active release 和实际安装包/运行脚本哈希。只有使用可选离线附件时才补充 ZIP 与附带清单的 SHA-256。其余生成文件如下：

| 目录 | 回传文件 |
| --- | --- |
| `before`（既有安装时） | `shared-oms-inspection.json`、`local-runtime.json`、`tasks.json` |
| `local` | 上述三项，加 `oms-offline-acceptance.json`、`acceptance.json` |
| `qmt-readonly` | 上述五项，加 `qmt-quote-fixtures.json` |
| `staged`（执行注册时） | `shared-oms-inspection.json`、`local-runtime.json`、`tasks.json` |
| 人工补充 | QMT 版本、采样时点/场景、实际交易规则来源、未覆盖项目说明 |

账户只传 SHA-256 指纹。不要回传 `.env`、API key、原始任务 XML、SQLite DB、spool、HOLD 正文、完整 broker 诊断日志或账户截图。失败时 `local-runtime.json` 可能只有 `inspection_failed` 和 `error_type`，将该文件与当前失败阶段一起回传；脚本会抑制可能带私值的提供方错误正文。

完成此清单表示“代码包、Windows 原生脚本、本地状态和 QMT 只读接口已按记录验收”，不表示已启用生产调度、完成服务端发布或获准真实交易。

## 9. 失败与回滚纪律

安装失败时，安装器尝试恢复旧代码指针和运行脚本，任务仍停用；**不会自动恢复 DB、spool 或 HOLD，不会重新启用旧调度**。成功安装后需要退回旧代码，也应先保持所有任务停用、保存新增证据，并由工程侧核对新旧协议和 schema 兼容性后给出明确的代码回退步骤。

不能把升级前 SQLite 覆盖回去来消除 UNKNOWN/SUBMITTING，因为券商侧的委托或成交不会随文件回退。不能同时启用两套调度，不能让旧客户端接管新协议产生的订单。任何未知委托先按账户指纹和订单标识完成 broker 事实核对，再决定恢复；本文件没有自动恢复或恢复交易命令。

开发机已运行静态 PowerShell token/here-string 检查和嵌入 Python 的合成测试，验证双库 WAL 备份、spool/HOLD 保留、错误绑定拒绝和只读检查。**开发机没有 PowerShell，静态测试不等于 Windows PowerShell 5.1 原生解析或执行通过。** 本文的目标机 parser/安装/只读连接结果是交接仍需回传的证据。

## 10. 可选离线附件

仅在 Windows 无法通过既有 GitHub 连接取得固定版本时，使用与 `bcdfd166faf13281c535d514c73d809570231ac9` 对应的离线 ZIP。它是同一功能基线的附件，不替代 GitHub/server 主交付记录。先核对发布方给出的 SHA-256；若附带 `RELEASE_MANIFEST.json`，同时核对其中 `source_commit`。不要使用 provisional 包，也不要根据文档新提交号推断功能版本。

展开到新的独立目录，保留 `v2.3\live_client` 层级，不覆盖运行目录或现有 Git 工作区。将 `$SourceCheckout` 改为该附件的展开根目录，设置 `$SourceRoot = Join-Path $SourceCheckout "v2.3"`、`$SourceWindows = Join-Path $SourceRoot "live_client\windows"`，`$SourceCommit` 仍显式设为上述固定 SHA，然后沿用第 2–9 节。没有 `.git` 时不执行第 1 节的 Git HEAD/干净状态检查；回传附件校验结果代替 `source-worktree-status.txt`。安装器不读取附件的 `RELEASE_MANIFEST.json`，安装本身不依赖它存在。
