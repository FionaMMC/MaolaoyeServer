# Server 紧急强制生成订单

强制发生在 server 的订单生成阶段。人工指定指令可以跳过策略研究/审核和月度调仓周期，产出正常 `raw_signals → orders → order_signal_map` 记录；MiniQMT 继续调用原 `/orders`，使用原 query / preflight / submit / settle 流程。客户端无需新增模式或升级。

## 操作入口

- `POST /hydra/emergency/stage`：使用现有 live 账户 token。只生成标准订单信号，不调用券商、不立即推送委托。实际拉取和执行遵循现有客户端流程；盘中使用须触发客户端现有拉单和提交操作。
- `POST /hydra/emergency/resume`：紧急订单结算对账完成后恢复普通生成。被终止的旧目标/补单计划不复活，需要明确的新计划。
- `/docs` 提供完整请求 schema。现有 paper、trigger 和行情备份 token 无法调用紧急入口。

生成请求示例（将示例身份、日期、数量、价格和哈希替换成实际人工指令；示例不是待执行订单）：

```json
{
  "execution_domain": "live",
  "account_alias": "YOUR_LIVE_ALIAS",
  "instance_id": "YOUR_INSTANCE",
  "request_id": "incident-YYYYMMDD-001",
  "operator": "账户负责人",
  "reason": "本次紧急处置的具体原因",
  "confirm_emergency": true,
  "trade_date": "20260928",
  "execution_raw_sha256": "上一个交易日已上传原价包的64位SHA256",
  "execution_calendar_sha256": "已上传交易日历包的64位SHA256",
  "orders": [
    {"symbol": "510300.SH", "direction": "SELL", "quantity": 100, "limit_price": 4.000}
  ]
}
```

日期只允许当日（15:00 前）或下一个交易日，原价必须来自该执行日的上一交易日。同一 `request_id` 与同一内容重复请求返回原批次；更改内容返回冲突。操作人声明和实际鉴权 client 身份分别保留。审核豁免原因保存在 `emergency_executions` 和 attempt 风险快照，已有审核记录不被改写。

## 执行边界

保留账户/域隔离、归属账本、白名单/执行黑名单、价格偏移、可用资金和可卖持仓检查。客户端仍对 QMT 实时资金、可卖份额及重复提交作原有校验。未成交不会自动成为下一次紧急订单；如需再次处置，使用新的人工指令。

尚未被客户端领取的本策略普通订单可以原子终止，旧记录和 ID 保留。已领取、活动或结果未知的委托需先撤单、结算和对账。**现有客户端一天只接受一个冻结批次；如果当日已经领取过批次，server 返回 409，不能通过新信号静默覆盖本地冻结内容。** 此限制是现有接收协议，不是再次要求策略审核。

紧急事件期间普通 server 生成和补单暂停。次日紧急信号可直接通过原晚间 advance → query → preflight 流程；成交与日终对账仍走原接口。`resume` 需指定同一账户、实例、`request_id`、衔接原因及 `confirm_new_plan_required=true`。

## Shadow Hydra 停更排查

2026-09-27 生产确认：目标文件仍为 20260724，40 天期限于 20260902 到期，最后净值日期为 20260902；从 20260903 起日任务报告 stale target。日任务仅估值、V7.13 周任务仅更新 Base、现有 Hydra 月度研究只发布实盘执行计划，未更新该影子目标文件。

修复将目标有效性与已有持仓估值分开。过期目标不产生调仓；只用已接受持仓和可用行情继续估值，状态为 `stale_target`，告警保留。没有可验证旧持仓或行情过期仍阻止估值，不以现金余额或旧价格伪造新净值。补估值不补分红、也不声称恢复 Hydra 月度研究发布；新影子目标的发布链路仍需单独接入。
