# Hydra live client 审阅整改汇报（2026-09-04）

## 结论

本轮已在独立交付分支中完成代码整改，未部署到 `C:\hydra-live`，未修改私有配置，未连接真实 QMT，未提交真实订单，也未修改服务器实现。

整改后的生产边界为：前一晚联网冻结与预检，次日 09:10 仅使用本地冻结批次和 QMT 下单；收盘后使用 Hydra 专用的 attempt close/retry 生命周期，不再调用普通 `StrategyPipeline` trigger。

## 审阅意见与处理结果

| 审阅意见 | 处理结果 |
|---|---|
| 09:10 又运行 preflight，重新依赖服务器 | 已移除。09:10 wrapper 只调用 `submit`；Python client 自行校验本地批次 hash 与前一晚 PASS 回执。 |
| 16:00 调用普通 `/hydra/live/trigger` | 已移除 `trigger_pipeline.py --live`。16:00 改为 Hydra 专用 `retry`。 |
| 15:10 只有 trade-result，没有关闭 attempt | 新增 `settle-close`：终态回报成功后生成账户证据、在线对账并调用 `/hydra/attempts/close`。 |
| residual 没有形成下一日补单 | 新增 `retry`：仅当服务器 close 回执为 `RESIDUAL` 时调用 `/hydra/rebalances/retry`；`COMPLETE` 时不触达补单接口。 |
| wrapper 可能落到错误 Python | wrapper 先加载私有环境，再强制校验 `HYDRA_LIVE_PYTHON` 的绝对文件路径；取消普通 `python` 回退。行情备份同时校验 `HYDRA_LIVE_CODE_DIR` 的 HEAD 与 `HYDRA_LIVE_CODE_COMMIT`，并拒绝相关源码存在未提交改动。 |
| 行情上传失败仍可能报 completed | `market_push.py --live-backup` 在 QMT 不通、空数据或上传失败时返回非零；成功必须输出 `UPLOADED` 或休市日 `SKIPPED_NON_TRADING` 回执，wrapper 才通知成功。 |
| Windows 运行代码游离于 Git | 安装器现会将经过版本化的 runner 和四份 runtime scripts 安装到 `C:\hydra-live`，并在升级前备份原文件。 |
| 旧 trigger 与新 retry 可能同时运行 | 注册器默认发现旧任务即拒绝；只有显式使用 `-ReplaceLegacyTasks` 才会禁用旧 15:10/16:00/18:00 任务并注册新任务。 |
| 18:00 成功后没有自动创建 09:10 任务 | 只有在批次冻结且 preflight 返回 `READY_FOR_OFFLINE_SUBMIT` 后，才按下一交易日创建 `Hydra-Live-Submit-YYYYMMDD-0910`。同名同内容返回 `ALREADY_REGISTERED`，同名异内容拒绝覆盖。 |
| 15:10 瞬时失败可能让 attempt 卡住 | 仅 `settle-close` 设置失败后 5 分钟重试一次；09:10 submit 绝不自动重试。 |
| retry execution hash 未绑定目标 | 私有配置必须同时给出 execution SHA-256、target id、rebalance id；16:00 在打开 QMT 前同时核对 close 回执和本地冻结批次。 |
| QMT 异常连接可能遗留 trader | 构造、callback、start、connect、account 或 subscribe 任一阶段异常均清理已创建 trader；`close()` 同时清空所有 QMT 引用。 |

## 整改后的任务时间线

| 时间 | Windows 任务 | 行为与硬边界 |
|---|---|---|
| 15:10 | `Hydra-Live-SettleClose-1510` | 查询 QMT 累计委托终态，推送 `/trade-result`；保存账户证据 SHA-256；对账并关闭 Hydra attempt。活动或未知委托、账本差异均 fail-closed。失败后 5 分钟仅重试一次。 |
| 15:30 | `Hydra-Live-MarketBackup-1530` | 上传隔离的 live-QMT 行情备份；必须取得机器可读成功回执。 |
| 16:00 | `Hydra-Live-Retry-1600` | 读取 15:10 的带 hash close 回执。只有服务器明确返回 `RESIDUAL` 才请求 retry；订单数量与内容由服务器生成。 |
| 18:00 | `Hydra-Live-QueryPreflight-1800` | 获取下一交易日，执行 `query → preflight`。无订单是正常终态；有订单必须冻结批次并取得 `READY_FOR_OFFLINE_SUBMIT`，然后创建下一交易日 09:10 一次性任务。 |
| T+1 09:10 | `Hydra-Live-Submit-YYYYMMDD-0910` | 只执行 `submit`。不创建 HTTP client、不访问服务器；缺批次或缺 PASS 回执时在打开 QMT 前停止；任务不自动重试。 |

## 数据和状态证据

- `workflow_receipts` 保存 `close` 与 `retry` 的规范 JSON、SHA-256 和记录时间，防止同日不同结果被静默覆盖。
- reconciliation evidence 写入私有日志目录的 `evidence` 子目录，文件名包含交易日、attempt id 和内容 hash。
- 16:00 retry 使用同一已批准调仓周期的 `HYDRA_LIVE_RETRY_EXECUTION_RAW_SHA256`、`HYDRA_LIVE_RETRY_TARGET_ID` 和 `HYDRA_LIVE_RETRY_REBALANCE_ID`。execution hash 来自 canonical raw manifest，两个 ID 来自 Server stage 响应或冻结批次；三者是不可分割的绑定，缺一即 fail-closed，它们不是账户密钥。
- 无当日批次时，15:10 返回 `NO_ORDERS`，16:00 返回 `NO_ATTEMPT`，不会在普通交易日制造误报警。
- 服务器返回 `COMPLETE` 时，16:00 返回 `NO_RESIDUAL`，不会打开 QMT 或请求补单。

## 代码范围

- `live_client/cli.py`：新增 `settle-close` 与 `retry` 工作流。
- `live_client/state.py`：新增不可变工作流回执。
- `live_client/http_client.py`：增加 Hydra retry 专用调用。
- `live_client/config.py`：校验 retry 所需 execution-raw hash。
- `live_client/gateway.py`：只在任何券商调用前重试 MiniQMT 连接，不重试 `order_stock`。
- `client/market_push.py`：失败退出码和机器可读上传回执。
- `live_client/windows/*`：早晚 wrapper、四任务注册器、固定 Python、版本化安装和回滚。

## 验证与准入

必须满足以下条件后才能部署：

1. Python/PowerShell 语法检查通过。
2. `live_client/tests/test_live_client.py` 全部通过，包括 mock 的 `query → preflight → submit → settle-close → retry`。
3. 客户端相关 test suite 为 45 passed、1 skipped；跳过项是 Windows 不支持用 Unix `0600` mode-bit 代表 ACL，该权限在部署时通过 Windows ACL 单独验证。
4. 从私有配置确认 `HYDRA_LIVE_PYTHON` 与本机 MiniQMT Python 一致。
5. 确认 `HYDRA_LIVE_CODE_COMMIT` 是本次获批的完整 Git SHA，且与 `HYDRA_LIVE_CODE_DIR` 的 HEAD 相同。
6. 从获批 canonical raw manifest 确认 retry execution SHA-256，并从同一周期的 Server stage 响应或冻结批次确认 target id 和 rebalance id；禁止猜测或混用不同批次。
7. 先安装新 release，但不注册任务；运行 `doctor` 与 mock acceptance。
8. 审阅计划任务动作后再替换旧的 15:10、16:00、18:00 任务。旧 `/hydra/live/trigger` 任务不得与新 retry 任务并存。

补充说明：从仓库根目录扫描整个 `v2.3` 时，服务器旧有的 6 个
`v53_adapter` 测试会因 Windows 默认 GBK 解码 UTF-8 `config.yaml` 失败；
本轮未改动 server/v53 文件，该问题与本次 live-client 整改无关，未为了
“全绿”扩大修改范围。

## 未做事项

- 未部署到当前实盘客户端。
- 未改动或重启服务器。
- 未更改 `C:\hydra-live\config\hydra-live.env`。
- 未清理现有计划任务。
- 未执行任何真实 QMT 查询、撤单或下单。

## 联合资金子账本复审补充

`64b8373` 已并入 `codex/hydra-strategy-capital-ledger-20260904`，因此正式部署
不能再单独使用客户端分支。联合复审确认：客户端 retry 请求不携带 residual、
订单、策略参考价或限价；Server 根据 attributed 策略持仓计算差额，并从获批
`hydra_execution_raw` 生成参考价和限价。新增回归测试固定了这条责任边界。

联合复审另外修正两处任务切换风险：

- 09:10 同名一次性任务只有在 restart count 为 0、`StartWhenAvailable=false`
  且重叠策略为 `IgnoreNew` 时才允许复用；否则拒绝把可能自动重下或迟到补跑的
  任务当作相同任务。
- 四个日常任务先全部以禁用状态注册并验证，再禁用旧任务、启用新任务；任何一步
  失败都会禁用本轮新任务并恢复原先启用的旧任务。

本机复审从 `v2.3/server` 正确运行目录执行完整 Server test suite，结果为退出码
0，只有缺少真实 V53 bundle 的一项集成测试按预期 skip。portable client 测试为
41 项通过；四项依赖 Windows MiniQMT `xtquant` 的测试未在 macOS 本机执行，沿用
原 Windows 复审的 45 passed / 1 skipped 证据，部署时仍必须由目标 Windows 再做
PowerShell 语法检查、installer doctor 和离线 acceptance。

联合部署与现有 21.1 万策略账本迁移的唯一执行顺序见
[`HYDRA_LIVE_UNIFIED_DEPLOYMENT_HANDOFF_20260904.md`](HYDRA_LIVE_UNIFIED_DEPLOYMENT_HANDOFF_20260904.md)。
