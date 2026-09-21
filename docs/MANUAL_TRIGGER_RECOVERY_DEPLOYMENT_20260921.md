# 模拟盘人工补跑 · 服务器上线回执

## 已上线

用户授权“完成测试就推 server”。2026-09-21 **19:50:15 Asia/Shanghai**，生产应用从 `c283e70276336160a9b55037c1605bdf9d223f75` 切换到：

`eedbde41d54b039c3401e1e46ccff70d596b99e0`

- GitHub：`codex/manual-trigger-recovery-20260921`。上线回执的后续文档提交不改变应用版本。
- 服务器路径：`/opt/qmt-server/v2.3/server`；生产分支 `codex/manual-trigger-recovery-release-20260921`。
- 新接口：`POST /admin/pipeline-jobs`、`GET /admin/pipeline-jobs/{job_id}`。
- 新 worker：`qmt-pipeline-recovery.service`；`qmt-pipeline-recovery.timer` 已启用。
- 已保留同期上线的 713 执行、影子精简和 Hydra 实盘看板改动；不是退回旧系统。

## 测试与数据保护

| 验证 | 结果 |
| --- | --- |
| 合并版本本地完整回归 | 920 passed、1 skipped，58.21 秒 |
| 合并交叉专项 | 44 passed |
| Linux 完整回归，使用生产应用依赖 | 920 passed、1 skipped，320.59 秒 |
| Linux systemd 校验、真实服务账号导入 | 通过 |
| 数据库副本迁移 | 28 张旧表逐表内容摘要不变，只新增空 pipeline_jobs |
| 副本 API/启动 | health、ready、dashboard、blueprint 正常，调度关闭 |
| 当前真实输入的 V53 副本补跑 | NO_ORDERS，0 单；峰值 RSS 217360 KiB，不是旧九笔恢复验收 |
| 生产发布前后核验 | 28 张旧表内容不变，新任务表为空，配置摘要不变 |
| 生产服务 | active/running，NRestarts=0，healthz=ok，readyz=ready |
| 新接口只读鉴权检查 | 未认证 401、paper 查询不存在任务 404、live 凭据 403 |
| 实际 worker 空闲启动 | success；数据库内容未变 |

Linux 测试工具安装在独立临时目录，**没有升级生产 Python 依赖**。回归进程最多 1 GiB 内存、一个 CPU、禁用 swap，网络禁止且生产应用目录只读。真实数据只在私有数据库副本测试中读取，没有上传原始账户或交易资料。

上线前已等待生成/估值服务退出，短暂停调度与 API 完成切换，随后恢复原先活跃的 7 个 timer。新旧生成代码没有并行运行。私有一致性备份及逐表摘要在服务器 `/var/backups/qmt-server/manual-recovery-deploy-l02kgncm`；不上传这些原始文件。

## 同伴下一步：只接入普通模拟盘入口

服务器端已经就绪，但 Windows **没有**因本次发布自动升级。原来的点击/计划任务若仍运行旧 `trigger_pipeline.py`，不会自动变成人工高优先队列。

1. 从上述 GitHub 分支取得客户端 `v2.3/client/trigger_pipeline.py`，核对应用提交 `eedbde41` 是其祖先或当前版本；保留原私有 `config.py`、HTTP 客户端配置及团队既有 Python 环境。不要因此切换 Hydra live_client。
2. 同伴决定实际补跑时，用既有已配置的 Python，在普通模拟盘 client 目录运行：

   ```powershell
   python trigger_pipeline.py --manual --account-group paper_v53 --wait-seconds 120
   ```

   示例中的 `python` 代表团队已配置的解释器，不是允许回退系统 Python。默认沿用 QMT 交易日历确定执行日。
3. 看明确状态：READY/REUSED 才表示有已发布批次可按原 order_query 下载；NO_ORDERS 表示本轮没有订单；退出码 3 表示仍在后台处理，可查同一任务，不应强制重建。BLOCKED/FAILED 的原因需要按回执处理。

本次**没有**调用生产 POST 来做烟测，没有 GET /orders，没有生成/领取/重发九月旧单，没有连接 QMT 下单，也没有修改 Windows 任务。服务器具备补跑能力，不等于 Windows 已接通，不等于历史调仓已恢复，更不承诺停电、缺输入时必然出单或成交。

## 运行边界与回退

- 新 worker 保留初始预算 MemoryHigh=768M、MemoryMax=1G、swap=0、CPUQuota=100%、10 分钟超时。当前非调仓日输入验证不代表已证明所有月末模型的峰值容量；其他训练进程的隔离仍另行整改。
- 九月 V53 缺终局及旧目标、上传目录权限、分红登记/重算等问题未在本发布中伪装解决。
- 如需回退，先停接收新补跑请求和 recovery timer，等待 worker 结束并保留任务/批次证据，再回退代码到 `c283e702`。保留新增表；**不得用备份数据库覆盖后续真实资金/成交**。已有新任务发布批次时，应先核对旧代码会否重算相同日期，不能直接混跑。
