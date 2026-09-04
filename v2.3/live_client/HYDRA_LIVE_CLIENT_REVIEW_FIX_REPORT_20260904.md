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

## 整改后的任务时间线

| 时间 | Windows 任务 | 行为与硬边界 |
|---|---|---|
| 15:10 | `Hydra-Live-SettleClose-1510` | 查询 QMT 累计委托终态，推送 `/trade-result`；保存账户证据 SHA-256；对账并关闭 Hydra attempt。活动或未知委托、账本差异均 fail-closed。 |
| 15:30 | `Hydra-Live-MarketBackup-1530` | 上传隔离的 live-QMT 行情备份；必须取得机器可读成功回执。 |
| 16:00 | `Hydra-Live-Retry-1600` | 读取 15:10 的带 hash close 回执。只有服务器明确返回 `RESIDUAL` 才请求 retry；订单数量与内容由服务器生成。 |
| 18:00 | `Hydra-Live-QueryPreflight-1800` | 获取下一交易日，执行 `query → preflight`。无订单是正常终态；有订单必须冻结批次并取得 `READY_FOR_OFFLINE_SUBMIT`。 |
| T+1 09:10 | 单次 submit 任务 | 只执行 `submit`。不创建 HTTP client、不访问服务器；缺批次或缺 PASS 回执时在打开 QMT 前停止。 |

## 数据和状态证据

- `workflow_receipts` 保存 `close` 与 `retry` 的规范 JSON、SHA-256 和记录时间，防止同日不同结果被静默覆盖。
- reconciliation evidence 写入私有日志目录的 `evidence` 子目录，文件名包含交易日、attempt id 和内容 hash。
- 16:00 retry 使用目标批准的 `execution_raw` SHA-256。部署前需要把该值写入私有变量 `HYDRA_LIVE_RETRY_EXECUTION_RAW_SHA256`；它是数据内容 hash，不是账户密钥。
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
3. 完整 test suite 为 38 passed、1 skipped；跳过项是 Windows 不支持用 Unix `0600` mode-bit 代表 ACL，该权限在部署时通过 Windows ACL 单独验证。
4. 从私有配置确认 `HYDRA_LIVE_PYTHON` 与本机 MiniQMT Python 一致。
5. 确认 `HYDRA_LIVE_CODE_COMMIT` 是本次获批的完整 Git SHA，且与 `HYDRA_LIVE_CODE_DIR` 的 HEAD 相同。
6. 从获批 target sidecar 确认 `HYDRA_LIVE_RETRY_EXECUTION_RAW_SHA256`，禁止猜测或取“最新文件”。
7. 先安装新 release，但不注册任务；运行 `doctor` 与 mock acceptance。
8. 审阅计划任务动作后再替换旧的 15:10、16:00、18:00 任务。旧 `/hydra/live/trigger` 任务不得与新 retry 任务并存。

## 未做事项

- 未部署到当前实盘客户端。
- 未改动或重启服务器。
- 未更改 `C:\hydra-live\config\hydra-live.env`。
- 未清理现有计划任务。
- 未执行任何真实 QMT 查询、撤单或下单。
