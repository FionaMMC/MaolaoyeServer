# Hydra 执行核心上线手册（目标 10/8）

代码分支：`feat/oms-live-core`。设计文档：`docs/superpowers/specs/2026-10-02-hydra-execution-core-design.md`；实施计划：`docs/superpowers/plans/2026-10-02-hydra-execution-core.md`。

**原则**：
- 服务器上的每一步操作，都要先得到用户确认才执行。
- 开关 `QMT_OMS_LIVE_ENABLED` 在 10/8 上午把关通过之前保持关闭。
- 任何一步不通过，就顺延到 10/12 卖、10/13 买。

## 已完成的验证（10/3）

| 层级 | 内容 | 结果 |
|---|---|---|
| 单元测试 | 计划器、状态机、账本、周期、接口、守卫、运维脚本 | 全部通过 |
| 计划器与回测同源 | 回测改为调用生产计划器后，在服务器上复跑 68 组 | 逐项相同 |
| 系统级回放（合成行情） | 真实服务器 + 真实代理 + 仿真 QMT，覆盖卖出保护价、买入限价、盘中触价、容量部分成交、资金不足 | 和研究回测逐笔一致 |
| 故障注入 | 服务器宕机、代理崩溃重启、下单结果不明、成交查询无响应、事件重复上传、日终快照缺失 | 没有重复下单，账本和券商一致；快照缺失时进入暂停 |
| 系统级回放（真实行情） | 服务器研究数据，近期 22 个周期 132 笔成交；全期 81 个周期 532 笔成交 | 完全一致，现金差 7×10⁻¹² 元 |

## 一、Windows 端（同事操作）

1. **拉取代码，用现有的安装脚本安装**：`Install-HydraLiveClient.ps1`，release 填本分支的提交号。安装后 `C:\hydra-live\scripts\` 下会多出 `Run-HydraOms.ps1` 和 `Register-HydraOmsTasks.ps1`。
2. **导出现有计划任务（只读）**：

   ```powershell
   reports\hydra_october_20261002\handoff\tools\Inspect-HydraTasks.ps1 -OutputDirectory C:\private\hydra-october\tasks-1007
   ```

3. **休市检查**：
   - 实盘账户，只读：

     ```powershell
     python -m live_client.oms_holiday_check --output C:\private\hydra-october\holiday-live.json
     ```

   - 模拟账户，单独准备一份指向模拟账户的环境文件：

     ```powershell
     python -m live_client.oms_holiday_check --output C:\private\hydra-october\holiday-sim.json --reject-probe --confirm-simulation-account <模拟账户号>
     ```

     这一步会发一笔价格 0.001 的必然被拒的单，用来记录三件事：下单返回值、券商保存了委托备注的几个字符、查询能不能查到之前几天的委托。**禁止对实盘账户执行这一步。**
   - 把两份 JSON 结果交给用户。
4. **9/30 正式月度输入**：运行 `python -m live_client.data_snapshot --as-of 20260930 ...`，具体参数见 `reports/hydra_october_20261002/HANDOFF.md`，然后上传到服务器 `private/hydra-incoming/`。
5. **10/7 晚上**，服务器部署完成、清单也批准之后，注册新任务并停用旧任务：

   ```powershell
   C:\hydra-live\scripts\Register-HydraOmsTasks.ps1 -InspectionDirectory C:\private\hydra-october\tasks-1007 -DisableLegacyTasks
   ```

   旧的 `Hydra-Live-*` 任务只是停用、不删除。回滚时把它们重新启用即可。

| 新任务 | 时间 | 内容 |
|---|---|---|
| Hydra-Oms-Pre-0900 / -1445 | 09:00 / 14:45 | 开盘前快照与对账，缓存当日计划 |
| Hydra-Oms-Buy-0914 | 09:14 | 等到 09:15:05 报入开盘集合竞价，之后每 30 秒查一次，直到 10:00 |
| Hydra-Oms-Cancel-1455 | 14:55 | 撤掉本系统没成交的买单 |
| Hydra-Oms-Sell-1456 | 14:56 | 等到 14:57:05 报入收盘集合竞价，之后查询到 15:01 |
| Hydra-Oms-Eod-1505 / -1530 | 15:05 / 15:30 | 日终快照（取两次） |
| Hydra-Oms-Upload-1800 | 18:00 | 补传服务器宕机期间存在本地的快照 |

**急停**：在 `oms-agent.db` 所在目录新建一个名为 `HOLD` 的文件，代理就会拒绝一切提交，不依赖服务器。

## 二、服务器端（每一步都要用户确认后才执行）

1. **预发布演练**：在生产数据库副本上，用端口 18001 起一个临时服务；代理接仿真交易所，完整走一遍。生产服务不重启、不改动。
2. **10/7 部署**：
   1. 备份 `pipeline-server.db`；
   2. 把代码导出到 `/opt/qmt-server/releases/oms-<sha>/`；
   3. 用 systemd 附加配置把 `qmt-server.service` 指向新目录，`.env` 里加上 `QMT_OMS_LIVE_ENABLED=false`；
   4. 重启服务，检查 healthz 和 readyz。
3. **发布十月目标**：

   ```bash
   PYTHONPATH=. python -m scripts.oms_ops publish ... --weights w.json --closes c.json --calendar cal.json
   ```

   - 先不带 `--apply` 预演，打印逐股清单；
   - 用户批准清单后，加 `--apply` 正式发布；
   - 最后执行 `approve --cycle-id C00001 --approver <用户> --yes`。

## 三、10/8 当天

| 时间 | 动作 | 通过条件 |
|---|---|---|
| 09:00 | 两个账户做快照和对账 | 对账通过 |
| 09:15 | 模拟账户用新代理跑一个小周期：买 100 份，挂单、成交、14:55 撤单 | 状态和成交回报正确 |
| 09:31 | 实盘金丝雀：一笔远低于市价、不可能成交的买单，回读委托备注和状态后撤掉 | 委托备注完整，撤单成功 |
| 上午 | 用户确认后打开开关，重启服务 | `/oms/live/plan` 返回 `executable=true` |
| 14:45 → 14:57 → 15:05 | 对账 → 实盘卖出 → 日终 | 卖单全部进入终态，对账通过，生成 10/9 买入计划 |
| 10/9、10/12、10/13 | 按日程执行 | 10/13 日终周期关闭，生成执行报告 |

## 四、暂停与回滚

- **对账暂停（HELD）**：查看 `python -m scripts.oms_ops status --account hydra-live`，查明原因。如果是快照缺失，先在 Windows 端运行 `upload-spool`，或者重新执行 `eod`。确认无误后执行 `oms_ops resolve --yes`。
- **回滚**：
  1. 关闭开关（`QMT_OMS_LIVE_ENABLED=false`），重启服务；
  2. Windows 端停用 `Hydra-Oms-*` 任务；
  3. 已经发出的委托以券商为准。

  不要重新启用旧链路去"接着跑"：旧链路不认识新核心的订单。
