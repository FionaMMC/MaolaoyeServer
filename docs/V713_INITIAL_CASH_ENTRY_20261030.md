# V713 首次现金建仓：操作交接

日期：2026-10-30；本文交易时刻均为北京时间。对应改动为显式首次建仓入口，部署版本与检查结果见本轮部署回执。普通换仓与 Hydra 不使用这个入口。

## 适用范围

`publish-v713-stock-leg --initial-cash-buy` 只适用于 V713 股票腿 attributed 账本：现金为正、持仓为空、没有历史周期／目标／月度状态或该策略订单历史，且没有未归属的活动订单责任。共享券商账户中已有 Hydra 持仓不影响该资格。

首次股票目标须为 TOP50 或 T1；AUX、已有持仓的换仓以及 AUX 后重新入场仍按原流程处理。不要把新参数加到 Hydra 或旧的混合 V713 发布命令上。省略新参数时，原 SELL→BUY 规则保持不变。

## 本轮预期日期

使用有效 as-of `20261030`、decision date `20261102` 的股票篮子，提供 10/30 有效不复权收盘证据，并在 **11/2 09:00 前完成发布和审批**：

- 首个时段为 **11/2 BUY**，没有 11/2 空 SELL；Windows 09:14 启动，提交基线 09:15:05。
- 十个交易日的执行窗口从首买日计起，每个交易日只补冻结目标的剩余数量，包括跨周末后的交易日。
- 后续买入必须等前一个已安排买入日的合格 EOD；缺日终证据时进入 HELD，缺证券收盘价时停止该证券的补买，不冒用旧参考价。
- 11/2 09:00 起新发布会顺延，须提供新的首买日前一交易日收盘证据；例如 11/2 收盘后发布可安排 11/3 BUY，并使用 11/2 收盘价。上午尚无当日正式收盘价时不能构造该证据。
- 已创建的首买周期若错过首买日 09:00 审批截止，会拒绝审批；审批不会自动改期，也不能回填时间、删除历史后重建。

这不是已发布的真实目标。必须等月末实际篮子及价格证据，再按预览审查真实证券、股数和现金。

## 收盘证据文件

新增 `--reference-close-evidence`，与 `--initial-cash-buy` 成对使用；不能用旧的平坦 `--reference-closes` 价格字典代替。

文件含 `trade_date`、`taken_at`、`quotes`。`quotes` 覆盖全部目标证券，每项含 `last_price`、带时区的 `source_time`、`adjustment="none"`、`price_source="raw_daily_close"` 或 `"raw_tick"`。价格必须为有限正数；来源时间须为对应交易日 15:00 后且不晚于采集时间，采集时间不得晚于发布。原始行情及证据留在服务器。

可以从真实 EOD 导出上述字段；若原快照尚未覆盖新目标股，应补齐真实原价来源，不能复制已有股票价格或修改复权标记。研究篮子本身的价格不能当作已验证的不复权收盘证据。若参考日就是信号日，证据价格还必须与 `--closes` 一致。

规范化证据、证据 SHA256、参考日期、周期及目标 ID 会随首次发布原子写入 `INITIAL_CASH_ENTRY` 审计回执。重复发布校验相同证据并返回已冻结日程；不会按新的当前时间重新排首买日期。

## 操作步骤

先核对股票腿实例、资金金额与初始化／划拨回执，不重复建账或划款。`V713_RELEASE_SERVER` 必须填本轮实际部署回执中的新 release 服务器目录，不能仍用 `b0d9358b` 或主仓库 `master`。

在服务器既有受控生产环境和 `/opt/qmt-server/v2.3/server` 工作目录执行预览；下方输入路径须替换为真实审查文件：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$V713_RELEASE_SERVER" \
/opt/qmt-server/venv/bin/python "$V713_RELEASE_SERVER/scripts/oms_ops.py" \
  publish-v713-stock-leg \
  --instance live_v713_stockleg --account hydra-live \
  --basket /remote/reviewed/v713_target_latest.parquet \
  --policy-template "$V713_RELEASE_SERVER/plugins/v713/stockleg_policy_template_v2.json" \
  --closes /remote/reviewed/unadjusted-signal-closes.json \
  --calendar /remote/reviewed/calendar.json \
  --initial-cash-buy \
  --reference-close-evidence /remote/reviewed/initial-close-evidence.json
```

1. 预览应显示 `initial_cash_buy=true`、正确 `reference_date`、首时段 BUY 和实际 `first_session_buys`；逐证券核对股数、价格、费用、现金预留及十个交易日窗口。无 `--apply` 不写数据库。
2. 负责人审查后，沿用完全相同的文件及哈希增加 `--apply`，创建 PENDING_APPROVAL 周期并保存实际 cycle ID。此时尚未自动批准或启用交易。
3. 使用同一 release 的 `oms_ops.py approve --cycle-id <实际ID> --approver <实际负责人> --yes`；必须在首买日 09:00 前完成。股票腿启用仍走原受控操作入口。
4. Windows 保持单一 shared OMS 执行器。首买日 09:00 PRE 必须成功并完成最新对账；09:14 任务再次取得当日有效授权。周五缓存的周一计划不能作为周一断网时的有效执行授权。
5. 首轮检查券商订单、实际成交、费用、归属账本及当日 EOD；部分成交只能补差。缺首日 EOD 或出现 UNKNOWN／对账失败时先处理证据，不直接恢复执行。

本轮软件发布不代替上述真实目标审批、交易启用或首轮成交验收。服务器与 Windows 的篮子检查、执行及报告任务时间不因这次修复而调整。

## 回退边界

尚未发布新首买周期时，可按本轮部署回执恢复原 `b0d9358b` 三个服务入口，保留原数据库和报告。新首买周期创建后，旧代码的 EOD 推进未具备本轮保护；不能直接让旧版接管该周期。必须先核实委托终态及周期责任，再确定恢复方案，不恢复到下单前数据库。
