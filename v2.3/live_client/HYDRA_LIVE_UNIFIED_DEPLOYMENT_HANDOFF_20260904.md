# Hydra 实盘统一部署、资金子账本迁移与 residual 交接

## 这次部署要达成什么

只部署客户端调度并不够，只部署资金子账本也不够。本次必须让 Server 和
Windows MiniQMT 客户端运行同一个获批提交，同时完成以下三件事：

1. Hydra 只能使用约 21.1 万元的策略资产，以及这些资产后续产生的成交盈亏、
   费用和已核实现金流；QMT 账户里临时存在的约 1,900 万元归入未分配储备，
   不得成为 Hydra 的购买力。
2. T+1 09:10 只读取前一晚冻结并通过 preflight 的本地批次后连接 MiniQMT，
   不访问 Server，也不自动重试下单。
3. 未成交补单由 Server 计算和定价：本地只回传成交与账户快照、请求 retry、
   拉取 Server 生成的新订单；本地不计算 residual、数量、策略参考价或限价。

统一交付分支是：

```text
codex/hydra-eod-cancel-lifecycle-20260904
```

只部署本交接消息最终给出的完整 40 位 commit。不要单独部署 `64b8373`，因为它
没有资金子账本；不要停在 `0c61d812`，因为它没有最后一轮一次性任务整改；也不要
继续使用 `1daf3334`，因为它没有 14:55 正式撤单和券商 16:00 终态回报时序。

本次日终修复的简版部署步骤见
`HYDRA_LIVE_EOD_CANCEL_HANDOFF_20260904.md`；与本文冲突时，以日终修复文档中的
14:55/16:05/16:20 时序为准。

## 责任边界

| 环节 | Server | Windows / MiniQMT |
|---|---|---|
| 目标与可用资金 | 使用 Hydra `virtual_cash`、归属持仓和策略自己的盈亏 | 上报完整 QMT 只读快照，不把全账户现金当 Hydra 现金 |
| residual | 用目标持仓减 Hydra 归属持仓 | 不计算，只读取带 hash 的 close 回执 |
| 补单价格 | 从获批的 `hydra_execution_raw` 冻结数据重新定价并生成订单 | 不传 residual、参考价、限价或订单内容 |
| 成交质量 | 保存策略参考价、到达价、成交 VWAP、滑点、IOPV 溢价和费用 | 从 QMT 采集并回传实际成交字段 |
| 09:10 下单 | 可以宕机；不在下单链路内 | 只校验本地冻结批次、preflight 回执和实时 QMT 账户/购买力 |
| 14:55 撤单 | 不参与，不改变订单状态 | 只对本地冻结批次中身份完全匹配且仍活动的 Hydra 委托请求撤单 |
| 16:05 结案 | 入账成交，维护策略子账本，计算 residual | 等待券商 16:00 正式回报后 settle-close；进程故障最多 5 分钟后重试一次 |
| 16:20 retry | `/hydra/rebalances/retry` 生成差额批次 | 只在 Server close 回执为 `RESIDUAL` 时请求 |

`15:30` 的 `live_qmt_backups` 是诊断备份，明确不能进入策略数据仓，也不能拿来
给补单定价。retry 使用的是另行获批并进入 canonical data store 的
`hydra_execution_raw` hash。

## 部署前填写并双人核对

```text
DEPLOY_COMMIT=<本交接消息给出的完整 40 位 SHA>
SERVER_OLD_COMMIT=<部署前 server git rev-parse HEAD>
CLIENT_OLD_RELEASE=<C:\hydra-live\config\active-release.txt>
SERVER_DB=<从 QMT_DB_URL 确认的实际 SQLite 文件>
HYDRA_INSTANCE_ID=live_hydra_v481_rb
HYDRA_ACCOUNT_ALIAS=<现网实际 alias>
HYDRA_APPROVED_CASH_BEFORE_TEMP_DEPOSIT=210589.85
HYDRA_APPROVED_POSITIONS={"510300.SH":100}
TEMP_DEPOSIT_JOURNAL_AMOUNT=<从 cash_flow_journal 查得的准确值>
RETRY_EXECUTION_RAW_SHA256=<获批 canonical raw manifest 的 hash>
RETRY_TARGET_ID=<Server stage/冻结批次的 target_id>
RETRY_REBALANCE_ID=<Server stage/冻结批次的 rebalance_id>
```

已知快照 `19,148,943.85 - 210,589.85 = 18,938,354.00`，所以
`18,938,354.00` 是临时入金的重建候选值，不是操作授权。最终必须以 Server
`cash_flow_journal` 中实际入账的那条 `DEPOSIT` 金额、事件 ID 和证据为准。

target/rebalance ID 来自 Server stage 响应或本地冻结批次；execution raw hash
来自获批 canonical raw manifest。三者必须属于同一调仓周期，但并不是三者都能
从原始研究 sidecar 中直接得到。缺任何一项时，先不启用 16:20 retry，不要猜值。

## A. 维护窗口与只读快照

在无 Hydra 任务运行、无活动/未知委托时操作。不要在 09:10 下单窗口内升级。
由同一位操作人完成 Server 和 Windows 切换，另一位只复核记录。

Server 先记录：

```bash
cd /opt/qmt-server
git status --short
git rev-parse HEAD
systemctl status qmt-server --no-pager
```

Windows 先记录：

```powershell
Get-Content C:\hydra-live\config\active-release.txt
Get-ScheduledTask -TaskName 'Hydra*' |
  Select-Object TaskName, State, TaskPath
Get-Process python*, powershell* -ErrorAction SilentlyContinue
```

如 Server 有已跟踪但未提交的修改，停止部署，先保存 diff 并由代码所有者判断；
不要直接覆盖。把现有 Windows 任务导出到私有备份目录，并把
`HYDRA_LIVE_TRADING_ENABLED` 暂时设为 `false`。这不会撤单；若 QMT 仍有活动委托，
先按券商状态处理完再进入维护。

## B. Server 部署与数据库迁移

先在 Server 获取并核对获批提交：

```bash
cd /opt/qmt-server
git fetch origin --prune
git cat-file -e '<DEPLOY_COMMIT>^{commit}'
test "$(git rev-parse '<DEPLOY_COMMIT>')" = '<DEPLOY_COMMIT>'
git status --porcelain --untracked-files=no
```

最后一条必须为空。然后停止服务、备份数据库和配置；备份放在生产工作树之外：

```bash
systemctl stop qmt-server
install -d -m 0700 /var/backups/qmt-server
cp -a <SERVER_DB> /var/backups/qmt-server/pipeline-server.pre-ledger-20260904.db
cp -a /opt/qmt-server/v2.3/server/.env \
  /var/backups/qmt-server/server-env.pre-ledger-20260904
sha256sum /var/backups/qmt-server/pipeline-server.pre-ledger-20260904.db
git switch --detach '<DEPLOY_COMMIT>'
```

先在数据库副本上连续演练两次；两次都必须成功：

```bash
cd /opt/qmt-server/v2.3/server
cp /var/backups/qmt-server/pipeline-server.pre-ledger-20260904.db \
  /tmp/hydra-ledger-rehearsal.db
/opt/qmt-server/venv/bin/python -m scripts.migrate_db \
  --db-url sqlite:////tmp/hydra-ledger-rehearsal.db --skip-stale-cleanup
/opt/qmt-server/venv/bin/python -m scripts.migrate_db \
  --db-url sqlite:////tmp/hydra-ledger-rehearsal.db --skip-stale-cleanup
```

`--skip-stale-cleanup` 是为了让本次只改 schema，不顺手删除历史示例状态。演练通过
后迁移生产库：

```bash
/opt/qmt-server/venv/bin/python -m scripts.migrate_db --skip-stale-cleanup
systemctl start qmt-server
systemctl status qmt-server --no-pager
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
journalctl -u qmt-server -n 100 --no-pager
```

此时只完成代码/schema 部署，不会自动生成或提交订单。保持账户初始化闸门关闭；
已有 `live_hydra_v481_rb` 不再走 initialize。

## C. Windows 私有配置与版本化安装

先备份私有配置，再只新增/更新以下非密钥字段；原 API key、账户 ID、fingerprint、
HTTP 业务批准和 webhook 不要发到聊天或 Git：

```text
HYDRA_LIVE_LEDGER_MODE=attributed
HYDRA_LIVE_INITIAL_ALLOCATED_CASH=210589.85
HYDRA_LIVE_INITIAL_ALLOCATED_POSITIONS_JSON={"510300.SH":100}
HYDRA_LIVE_RETRY_EXECUTION_RAW_SHA256=<同周期获批 canonical raw hash>
HYDRA_LIVE_RETRY_TARGET_ID=<同周期 target_id>
HYDRA_LIVE_RETRY_REBALANCE_ID=<同周期 rebalance_id>
HYDRA_LIVE_CODE_COMMIT=<DEPLOY_COMMIT>
HYDRA_LIVE_TRADING_ENABLED=false
```

`INITIAL_ALLOCATED_*` 对已有实例不会重置 Server 账本，只是保留当前获批开账依据；
以后创建新策略时必须分别填写其现金和归属持仓，不能把“总资本”全部填进 cash。

在 Windows 代码目录核对并安装：

```powershell
$HydraDeployCommit = '<DEPLOY_COMMIT>'
Set-Location C:\src\MaolaoyeServer
git fetch origin --prune
git cat-file -e "$HydraDeployCommit`^{commit}"
if ((git status --porcelain --untracked-files=no)) { throw 'Tracked worktree is dirty' }
git switch --detach $HydraDeployCommit
if ((git rev-parse HEAD).Trim() -ne $HydraDeployCommit) { throw 'Wrong deploy commit' }

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\v2.3\live_client\windows\Install-HydraLiveClient.ps1 `
  -SourceRoot C:\src\MaolaoyeServer\v2.3 `
  -InstallRoot C:\hydra-live `
  -PythonExe <MiniQMT Python 的绝对路径> `
  -SourceCommit $HydraDeployCommit
```

安装器会版本化安装、备份旧 env/runner/SQLite、运行本地 doctor 和 mock 离线验收，
但不会连接真实 Server/QMT，也不会改任务。必须保存最终 JSON，并确认：

```text
status=INSTALLED
offline_acceptance=PASS
local_doctor=PASS
env_preserved=true
state_preserved=true
tasks_modified=false
```

再独立执行：

```powershell
C:\hydra-live\bin\Run-HydraLive.ps1 -Command doctor
```

必须看到 `ledger_mode=attributed`、`server_contacted=false`、`qmt_contacted=false`。

## D. 把临时 1,900 万从 Hydra 划回未分配储备

先只读查询 Server `cash_flow_journal`，确认临时 `DEPOSIT` 的准确金额、source、
source_event_id 和 evidence hash。不要用当前 QMT 余额倒推覆盖原记录。只有本步骤需要
`QMT_LIVE_CASH_FLOW_INGEST_ENABLED=true`；若现网为 false，由 Server 管理员在维护
窗口临时打开并重启。`QMT_LIVE_ACCOUNT_INITIALIZATION_ENABLED` 继续保持 false。

在 Windows 执行一条新的抵销流水；金额必须是已确认临时 DEPOSIT 的精确负数：

```powershell
C:\hydra-live\bin\Run-HydraLive.ps1 -Command cash-flow `
  -Date 20260904 `
  -CashFlowType CAPITAL_DEALLOCATION `
  -Amount -<临时 DEPOSIT 的准确金额> `
  -Source owner-capital-allocation `
  -SourceEventId hydra-temp-cash-unallocate-20260904-v1 `
  -EvidenceSha256 <包含原流水与21.1万归属计算的审批文件SHA256> `
  -Description 'Keep temporary account cash unallocated; Hydra remains at approved capital' `
  -TransitionToAttributed
```

这条操作不把钱从 QMT 转走，只把它从 Hydra 的策略购买力中移除。原 `DEPOSIT` 和新
`CAPITAL_DEALLOCATION` 都保留，重复相同 event ID/内容是幂等的，改内容会被拒绝。

随即查询：

```powershell
C:\hydra-live\bin\Run-HydraLive.ps1 -Command ledger
```

若这期间没有其他 Hydra 成交/股息/费用，`ledger` 预期是：

```text
ledger_mode=attributed
virtual_cash=210589.85
positions={"510300.SH":100}
```

`ledger` 接口只返回策略子账本，不返回全账户余额。随后一次不下单的 preflight 中，
还应看到 `physical_qmt_available_cash≈19148943.85` 和
`portfolio.unallocated_cash≈18938354.00`；两者不相等于 Hydra cash 正是预期结果。

如存在合法的后续 Hydra 事件，`virtual_cash` 应在 210,589.85 基础上只反映这些事件，
不能强行调成固定数字。白名单九只 ETF 的每一股物理持仓必须能分配到某个 attributed
策略；白名单外人工持仓可以作为 external position。验收后，如当前没有自动化且获批
的分红/划拨入账任务，应重新关闭 cash-flow ingest 闸门；以后只在有证据的入账窗口开。

## E. 任务切换

安装后先确认没有旧/新任务正在运行，导出旧任务 XML。第一次运行注册器时不带替换
开关，它若发现旧任务会只报错、不改现网：

```powershell
& C:\hydra-live\scripts\Register-HydraLiveOperationsTasks.ps1
```

复核报错列出的旧任务确为要替换的 15:10、16:00、18:00 任务后，再执行：

```powershell
& C:\hydra-live\scripts\Register-HydraLiveOperationsTasks.ps1 `
  -ReplaceLegacyTasks
```

注册器会先把五个新任务以禁用状态全部注册并验证，再禁用旧任务、启用新任务；任一
步骤失败会禁用本轮新任务并恢复原先启用的旧任务，避免半套新旧链路并存。必须确认：

- `Hydra-Live-CancelOpen-1455`：无自动重启；14:57 后启动会报警且不发撤单；
- `Hydra-Live-MarketBackup-1530`：无自动重启；
- `Hydra-Live-SettleClose-1605`：等待券商 16:00 最终回报，失败后 5 分钟最多重启一次；
- `Hydra-Live-Retry-1620`：无自动重启，不含 `trigger_pipeline.py --live`；
- `Hydra-Live-QueryPreflight-1800`：无自动重启；
- 旧 `Hydra-Live-SettleClose-1510`、`Hydra-Live-Trigger-1600` 和
  `Hydra-Live-Retry-1600` 已禁用；
- 不存在固定每日 09:10 submit 任务。

`cancel-open` 不是“收盘后清账”，而是在券商截止时间前发送真实撤单请求。它只处理
本地 state DB 中本批次 `SUBMITTED` 的订单，并在任何外部写操作前逐笔核对 QMT
账户、代码、数量、限价、方向、`strategy_name=hydra_live` 和确定性 remark；任一
身份不符时整批不撤。QMT 返回 0 只记为 `REQUESTED`，不能写成 `CANCELLED`。
`ORDER_REPORTED_CANCEL` 与 `ORDER_PARTSUCC_CANCEL` 都仍是活动状态，必须等券商在
16:00 后回报 `ORDER_CANCELED`、`ORDER_PART_CANCEL`、`ORDER_SUCCEEDED` 或
`ORDER_JUNK`，16:05 才允许 settle-close。部分撤单请求失败时保存
`CANCEL_INCOMPLETE` 和 SHA-256 evidence 并报警，不会撤账户中其他策略的订单。

18:00 只有在 `query → preflight` 返回 `READY_FOR_OFFLINE_SUBMIT` 后才创建一次性的
`Hydra-Live-Submit-YYYYMMDD-0910`。同名任务的动作、时间、用户或“禁止自动重试/
禁止错过后补跑”设置不同都会拒绝覆盖。

若部署当晚的 18:00 已经过，但次日已有获批订单，不能只手工注册 09:10。应在夜间
先手动运行 `Hydra-Live-QueryPreflight-1800`，确认它重新取得/复用冻结批次、preflight
为 READY 并自动生成一次性任务。不得在 09:10 临时联网补 preflight。

## F. 启用与首个完整周期验收

完成以下验收后才把 `HYDRA_LIVE_TRADING_ENABLED` 改回 `true`：

1. Server 和 Windows 记录的 full SHA 都等于 `<DEPLOY_COMMIT>`；
2. Server migration 两次演练和生产迁移成功，health/readiness 正常；
3. `ledger` 只显示 Hydra 约 21.1 万归属资产，约 1,900 万为 unallocated；
4. Windows installer、doctor、offline acceptance 全部通过；
5. 五个新日常任务正确，旧 15:10/16:00 任务已禁用；
6. retry 三元组来源已记录；没有 residual 时缺三元组不影响 09:10 主单，但 16:20
   会拒绝补单；
7. QMT 当前无活动/未知旧委托，本地没有 `SUBMITTING_UNKNOWN`。

首个周期应保存以下脱敏回执：

```text
18:00  FETCHED/ALREADY_FETCHED + READY_FOR_OFFLINE_SUBMIT + REGISTERED
09:10  submitted/attempted_now/recovered（同日第二次运行 attempted_now 必须为 0）
14:55  CANCEL_REQUESTED/NO_ACTIVE_ORDERS/NO_ORDERS 之一，并保存 SHA-256 回执
16:05  ATTEMPT_CLOSED，close.status 为 COMPLETE 或 RESIDUAL
16:20  NO_RESIDUAL/NO_ATTEMPT/ALREADY_STAGED/RETRY_STAGED 之一
```

若为 `RETRY_STAGED`，18:00 会拉取 Server 生成的差额批次，次一交易日 09:10 再离线
提交。客户端请求中不应出现 residual、reference_price、limit_price 或 orders；这些
由 Server 生成。用 `hydra_execution_quality_report.py` 核对策略参考价、到达价、
成交价、shortfall 和 premium。

## 停止条件与回滚边界

任一项发生时停止切换并保留证据：SHA 不一致、tracked worktree 非 clean、数据库
备份/迁移失败、ledger 不是 attributed、策略现金大于获批额度、归属持仓解释不通、
任务动作不同、QMT 有未知委托或本地状态为 `SUBMITTING_UNKNOWN`。

资金子账本尚未切换前，可以恢复旧 Server commit 和 Windows active-release 指针。
一旦 `CAPITAL_DEALLOCATION + TransitionToAttributed` 成功：

- 不要把 Server 回滚到不认识 attributed ledger 的旧提交；暂停时关闭客户端 trading
  和四个新任务，保留数据库，采用 forward-fix；
- 不删除新 schema、现金流、close/retry 回执、本地 SQLite 或成交记录；
- 不重新启用旧 `trigger_pipeline.py --live`；
- 代码指针回滚只允许回到同样支持 attributed ledger 和离线 submit 的已审版本；
- 任何后续增资使用新的 `CAPITAL_ALLOCATION`，分红使用 `DIVIDEND`，每条都带稳定
  source_event_id 和证据 hash。QMT 全账户余额变化永远不自动扩张 Hydra 额度。
