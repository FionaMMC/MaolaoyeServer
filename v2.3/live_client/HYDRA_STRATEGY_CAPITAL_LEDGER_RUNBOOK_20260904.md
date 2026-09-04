# Hydra 策略资金子账本部署与迁移

> Server、Windows、residual 调度和现有实例纠正的统一执行顺序见
> [`HYDRA_LIVE_UNIFIED_DEPLOYMENT_HANDOFF_20260904.md`](HYDRA_LIVE_UNIFIED_DEPLOYMENT_HANDOFF_20260904.md)。
> 本文保留资金口径说明；实际部署以统一 handoff 为准。

## 目的

一个 QMT 账户可以放多个策略和未分配现金，但 Hydra 只能使用：

```text
初始分配资本
+ Hydra 自己的交易盈亏
+ Hydra 持仓收到的股息
- Hydra 自己的交易费用
± 明确审批的后续资本划拨
```

QMT 全账户余额只用于证明物理资产完整，不再直接成为 Hydra 的 NAV、购买力
或可卖持仓。相同 ETF 可以由多个 attributed 策略共同持有；归属由订单和成交
血缘维护，收盘时校验所有策略持仓之和等于 QMT 物理持仓。

## 部署顺序

以下操作不会自动下单，但必须先备份 Server 数据库和 Windows 私有配置。

1. 将 Server 和 Windows live-client 同时部署到本文所在提交。不要只升级一端。
2. Server 停写或进入维护窗口后执行幂等迁移：

   ```bash
   cd /opt/qmt-server/v2.3/server
   venv/bin/python -m scripts.migrate_db
   ```

3. Windows 私有配置新增：

   ```text
   HYDRA_LIVE_LEDGER_MODE=attributed
   HYDRA_LIVE_INITIAL_ALLOCATED_CASH=210589.85
   HYDRA_LIVE_INITIAL_ALLOCATED_POSITIONS_JSON={"510300.SH":100}
   ```

   后两个值只供“全新实例首次初始化”使用。已有实例的实际额度始终以 Server
   当前子账本为准，不能靠改 env 重置。

4. 运行本地配置检查；它不连接 Server 或 QMT：

   ```powershell
   C:\hydra-live\bin\Run-HydraLive.ps1 -Command doctor
   ```

## 现有 Hydra 实例的纠正方式

不要删除或篡改昨晚那笔 `DEPOSIT`。从 Server 的 `cash_flow_journal` 找到该笔
误归属给 Hydra 的准确金额、`source_event_id` 和证据；纠正额应是该误记金额的
精确负数，不要用“约 1900 万”或用当前账户余额倒推。根据现有两个现金快照，
`19,148,943.85 - 210,589.85 = 18,938,354.00`，但这个差额只能用于交叉检查，
不能替代 journal 原记录和审批证据。

准备一份审批/核对文件并计算 SHA-256，然后在 Windows 连接真实 QMT 的环境运行：

```powershell
C:\hydra-live\bin\Run-HydraLive.ps1 -Command cash-flow `
  -Date 20260904 `
  -CashFlowType CAPITAL_DEALLOCATION `
  -Amount -<误记 DEPOSIT 的准确金额> `
  -Source owner-allocation-correction `
  -SourceEventId hydra-unallocate-20260904-v1 `
  -EvidenceSha256 <审批文件 SHA-256> `
  -Description 'Reclassify physical deposit as unallocated account reserve' `
  -TransitionToAttributed
```

这一步原子完成两件事：把误分配资金退回“未分配储备”，并把现有 Hydra 实例
切到 `attributed`。重复相同 `source_event_id` 和相同内容只会幂等返回；内容变化
会拒绝覆盖。

## 验收

运行：

```powershell
C:\hydra-live\bin\Run-HydraLive.ps1 -Command ledger
```

必须确认：

- `ledger_mode` 是 `attributed`；
- `virtual_cash` 等于误记入金发生前的 Hydra 现金，加上此后归属于 Hydra 的成交、
  费用、股息和盈亏变化；
- `positions` 只包含 Hydra 归属份额；
- `cash_flow_totals` 同时保留原 `DEPOSIT` 和新的 `CAPITAL_DEALLOCATION`，净效果可解释。

随后用一个不下单的目标批次走 `query → preflight`。preflight 回执必须同时出现：

```text
reconciliation_scope = portfolio_attributed
risk.capacity_scope = attributed
risk.qmt_available_cash = Hydra 子账本现金
risk.physical_qmt_available_cash = QMT 全账户现金
```

前者约为 Hydra 自身额度，后者可以是 1900 多万；两者不同是正确结果。09:10
离线 submit 只读取昨晚冻结的 Hydra 额度和本地批次，不访问 Server。

## 回滚边界

代码可以回滚到上一 release 指针，但数据库迁移列和资本流水不要删除。若迁移后
需要暂停，只关闭 Hydra 任务或 `HYDRA_LIVE_TRADING_ENABLED`；不要把实例改回
dedicated，也不要反向伪造一笔 `DEPOSIT`。任何再次分配额度都使用新的
`CAPITAL_ALLOCATION` 流水和当时 QMT 只读现金快照。
