# Hydra 本轮整改交接 / 2026-09-13

## 当前交付状态

本地目录：`/Users/mameican/Desktop/server`。

本轮独立审阅分支：`codex/hydra-remediation-review-20260913`。

基础提交：`0cbffcc62c36e4f40ed4f7b17c5a345514b8c7b5`，来自原审阅分支 `codex/hydra-trading-core-review-20260909`。本轮代码、测试及报告随新的独立审阅提交交付；基础 SHA 不包含本轮修复。请获取新分支并核对其完整 HEAD，不能只 checkout 旧 SHA。**本交付仅供审阅，未部署、未批准生产写账或切换任务。**

[GitHub 审阅分支](https://github.com/FionaMMC/MaolaoyeServer/tree/codex/hydra-remediation-review-20260913)

```sh
git fetch origin codex/hydra-remediation-review-20260913
git rev-parse FETCH_HEAD
git diff 0cbffcc62c36e4f40ed4f7b17c5a345514b8c7b5 FETCH_HEAD --stat
```

上述命令只获取和查看，不会切换当前工作区或部署。审阅时重点看：收入分账的金额守恒/去重、缺数据的恢复状态、Windows 现场补丁是否完整保留，以及报告中明确未覆盖的生产恢复和原生 Windows 验收。

生成回放所用实际源码摘要：`ff1a63d8101c03e5de2b396bc9dad115b25966b479f8fe445c6a0024bfbcba65`，177 个源码/配置文件；范围见 `frozen_generation.json.actual_worktree`。它覆盖回放当时尚未提交、现随本轮交付的服务代码；源码摘要不是可 checkout 的 Git 提交，也不是安装包签名。

生产只读核实的提交为 `eac6e3541bbf2cd850a32bdd8afe9d1c94830aaf`。Windows 当前安装版本和任务本轮未重新连接确认，不能用同伴历史报告代替当下版本核实。

## 已验证

| 检查 | 结果 | 边界 |
|---|---|---|
| Server 全部 tests/unit | 665 passed | macOS 隔离 SQLite，不是生产数据库迁移验收 |
| Live client tests | 138 passed | 不调用真实 QMT；PS 部分是源文件约束，不是原生解释器执行 |
| offline_acceptance | 4 类 PASS | mock QMT；服务器不可达、重复调用、已受理后崩溃、不确定响应 |
| 原始四流研究复算 | MATCH，9 权重差均 0 | aa6b60d；2026-09-03 冻结目标 |
| 原始四流 + 真实基线生成订单 | 9 笔，无字段差异 | 临时数据库；严禁提交诊断订单 |
| 历史结果结算/close | 六笔成交、三笔策略到期、正确 residual；重复不改账 | 9 月 4 日历史观测，非独立 QMT 账户对账；不含人工单入生产账 |
| Ruff / diff 空白检查 | 本轮新增工具/模块/测试及所检查修改文件通过 | 不是整个仓库历史代码全量 lint |

执行服务器测试（仓库根目录进入 server 后）：

```sh
cd v2.3/server
PYTHONPATH=.. venv/bin/python -m pytest tests/unit -q -o addopts=''
```

执行客户端测试和纯离线验收（仓库根目录）：

```sh
PYTHONPATH=v2.3 v2.3/server/venv/bin/python -m pytest v2.3/live_client/tests -q -o addopts=''
PYTHONPATH=v2.3 v2.3/server/venv/bin/python -m live_client.offline_acceptance
```

首次客户端测试曾因缺 `PYTHONPATH=v2.3` 无法收集，补环境后执行成功；缺分红、缺估值的旧测试契约也已更新为显式等待。新增对应恢复测试，非简单删掉失败断言。回放时 PyArrow 在沙箱中有 CPU 信息读取警告，最终退出码为 0、结果匹配。

## 本轮变更清单

- `v2.3/server/app/strategy/base.py`、`runner.py`、`app/scheduler/pipeline.py`：缺输入隔离、原业务日期等待状态及摘要。
- `v2.3/server/plugins/v53_adapter.py`：缺分红不退化原价，晚到/替换文件重读；调仓历史不足明确等待。
- `v2.3/server/app/services/perf.py`：缺持仓估值不按零市值发布 NAV。
- `v2.3/server/app/models/income_allocation_receipt.py`、`schemas/income_allocation.py`、`services/income_allocation.py`：收入多策略原子归属；`api/account_cash_observation.py` / `auth.py` 接入受限账户 API。
- `services/strategy_capital.py`、`services/cash_flow.py`：已归属收入释放待归属占用，保留已拥有现金；阻止旧接口重复登记同一原始收入事件。
- `v2.3/live_client/windows/Invoke-HydraLiveOperations.ps1`、`Run-HydraLive.ps1`：现场 Windows 热修语义合并、退出码立即捕获、编码及旧任务别名；保留新版 advance。
- `tools/collect_hydra_server_evidence.py`：只读服务器证据收集，输出须保密。
- `tools/replay_hydra_frozen_research.py`、`audit_hydra_generation_projection.py`、`hydra_source_identity.py`：真实冻结回放与实际工作区来源标记。

## 新收入归属接口的正确使用

仅在候选版部署并完成账务审阅之后使用。以下为协议示意，不是当前生产请求。

第一步：`POST /accounts/cash-observations` 接收券商实际到账事件，保留其唯一 source / source_event_id 和证据摘要。这一步不加策略现金。

第二步：确认该收入的历史权益归属后，调用 `POST /accounts/income-allocations`：

```json
{
  "execution_domain": "live",
  "account_alias": "YOUR_ACCOUNT_ALIAS",
  "observation_id": 123,
  "event_date": "20260908",
  "evidence_sha256": "REPLACE_WITH_64_HEX_ENTITLEMENT_EVIDENCE_HASH",
  "allocations": [
    {"instance_id": "hydra", "amount": "10.00"},
    {"instance_id": "kobold", "amount": "20.00"}
  ]
}
```

这里只演示实际到账 30 元、明确归属 10/20 元的情况。真实 instance_id、日期、现金事实 ID、证据必须取自真实材料。分配依据应说明权益登记日归属或可核验的约定，不能凭当前持仓比例臆测。

预期：返回 ALLOCATED；同样内容重试 `already_applied=true`；两策略原持仓不变，各加相应现金；合计正好 30 元；不生成订单。回执中的现金是**当时入账后的快照**，重试不把它冒充当前余额。

限制说明：

- 全额分配一次完成；归属不清的收入留在账户事实层，其自身金额暂不当作闲置资本。
- 入金不是收入：本金增减仍走 `/accounts/capital-movements`，不会因券商总现金增加就扩大 Hydra 规模。
- 旧接口已经入账的事件需要先迁移归属回执，不能再加一次钱；本轮未提供自动迁移/冲正入口。
- 没有自动登记日权益计算、应收分红资产、历史收益重算和 Windows 收入采集/分配任务。不得宣传成“所有分红会自行解决”。

## 生产账本恢复：先验证，后另行批准写入

这一步是当前迁移的重要前置，不是为了审计而阻止已知事实入账。

1. 取得 SQLite 一致性备份、当前 QMT 现金/持仓、9 月 4 日及 8 日完整委托/成交明细和最终费用。核实采集之后是否已有人修过账，避免重复恢复。
2. 在隔离数据库副本中保留原订单 ID、target/rebalance/attempt 身份。逐筆导入六笔原委托成交；不要为了适配新摘要重新生成历史订单。
3. 三笔人工委托保留其独立券商身份，明确归属 Hydra，并关联原调仓目的；不能伪装成原三笔未成交单的回报。
4. 盘后策略到期与券商原始状态分开存。完成资金/持仓归属和费用检查，再计算当前 residual；人工已补齐部分不得再买。
5. 重放同一恢复材料两次，第二次资金/持仓/成交记录不变；策略合计与独立 QMT 快照可解释，保留未分配账户资金。
6. 输出恢复前后差异、费用来源、人工归属和无法解释项，由双方批准后才写生产。不要使用 `SET cash=7149.15` 或直接重置仓位来掩盖明细缺失。

本轮服务器权限能取得服务器持有的资料，但不能从缺失的服务端回报凭空产生 Windows 原始日志、券商最终交割费用或原始盘口。这些少数证据仍需同伴从实盘电脑/券商补充。

## V53 九月事故的具体追查点

服务器记录九笔订单于 8 月 31 日 20:23:41 生成、20:23:46 标记已拉取，valid_date=20260901，现状态 EXPIRED。`fetched_at` 在服务器返回订单前写入，不能证明网络响应完整到达 Windows。

请查客户端：20:23 左右 query 是否得到成功返回、落在哪个文件/日期/目录；次晨任务读取的是否同一批次；是否被本地已执行日期、错误过期判断、旧缓存、任务动作/路径或连接失败跳过。保留命令行、退出码、落盘文件摘要与本地数据库状态。先定位，不默认 `force=True` 重算所有订单。

## 部署和回退的边界

先发布可核验的新提交和版本包，备份生产数据库、Windows 私有配置、状态库、任务定义。新收入回执表是增量建表，不需要删表/覆盖数据；但**已有账務不能因回退代码被抹掉**。

在副本和 Windows 验收后，先恢复已知账务，再协调服务器与客户端版本和任务切换。切换期间只短暂停发新委托，不妨碍读取券商事实。恢复自动链之前确认人工补齐记录已纳入，历史 residual 不再待执行。

如果已经产生新归属记录，不能只换回不认识这些回执的旧代码再重复导入现金；应保留事实和 journal，执行经核实的前向修复，或在无新事实丢失的前提下进行一致性回退。这个限制保护的是已发生的账务，不是禁止正常回退软件。

## 尚未在本轮完成

- 生产成交补记、最终费用核实、人工单归属迁移及真实 QMT 对账。
- Windows PowerShell 5.1 原生运行、当前任务盘点和新版安装验收。
- 全六类执行样本的持续采集、买一卖一到达价、真实费用函数和实盘校准回测。
- V53 原始客户端未执行事故的最终定位、七月历史账重建。
- 分红登记日权益/应收、冲正流程、旧收入归属回执迁移。
- 普通 pipeline 强制重算替换 ID 的结构性重构和跨日自动恢复。

这些不能用“全部测试通过”一笔带过，但也不需要阻止本轮已验证修复进入候选审阅。保持 50bp，下一次自然交易补充先卖后买的真实观测，不为测试制造真实委托。
