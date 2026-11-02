# V713 / Hydra 10 月末问题：证据记录

日期：2026-11-02 晚。全部为**只读**排查：读取了服务器日志、systemd 配置、生产数据库（只读打开）、发布目录和源码，没有修改服务器、客户端、任务或配置。本文补充同事整理的现场记录 `V713_HYDRA_MONTH_END_ISSUES_20261030.md`（尚未入库），每条结论附证据来源；仍需核实的事项单独标出。

## 一 结论摘要

| # | 问题 | 根因 | 影响 | 截止 |
|---|---|---|---|---|
| 1 | V713 月末当天生成不了篮子 | 选股程序按"最新数据所在月份"判断月份是否结束 | 9/30、10/30 两次被拦；V713 每月比设计晚一个交易日执行 | 11/30 月末前 |
| 2 | 2027 年 1 月底起无法生成篮子 | 交易日历的月末列表写死到 2026 年 | 从 2027-01-29 起每天被拦，不会自行恢复 | 2027-01-29 前 |
| 3 | Hydra 月度数据包没人生产 | 上传藏在旧任务"发布执行行情"里，升级停用旧任务后没有接替者 | 10 月底需人工补传；这条自动链路在生产上第一次跑通是 11/02 人工补传之后 | 11/30 月末前 |
| 4 | 生产代码分属多个版本，其中两个不在 GitHub | 每次部署单独叠一层发布目录和覆盖配置 | 无法从仓库复现生产行为；升级时容易漏掉依赖 | 统一修订时 |
| 5 | V713 篮子里的 Hydra 半边是冻结权重 | 读取 2026-06-30 的固定文件 | 影响模拟盘 V713 和 Base 影子，不影响实盘股票腿和合成账本 | 统一修订时 |

## 二 问题 1：V713 月份判断

### 现象

外层调度 `v2.3/server/scripts/v713_cycle.py`（9/21 随 `2b95276` 加入）在月末最后一个交易日收盘后判定"到期"，执行日取下一个交易日。生成篮子的是策略发布目录里的 `round4/v7.13/execution.py`，生成后由 `deploy/run_v713_weekly.sh:108` 比对数据日。日志（`/opt/qmt-refresh/logs/v713_weekly_*.log`）：

| 运行 | 外层预期 | 选股程序输出 | 结果 |
|---|---|---|---|
| 9/30 17:30、20:00 | as_of 0930，执行 10/08 | as_of **0831** | 被拦 |
| 10/08 17:30 | as_of 0930，执行 **10/09** | as_of 0930 | 安装 |
| 10/30 17:30、20:00 | as_of 1030，执行 11/02 | as_of **0930** | 被拦 |
| 11/02 17:30 | as_of 1030，执行 **11/03** | as_of 1030 | 安装 |

所以这不是 10/30 偶然出错，而是 9/21 以后的每个月末都会发生。第二个交易日那次运行"成功"了，把晚一天执行的问题盖住了。

### 根因

策略发布目录 `/opt/qmt-refresh/releases/small_cap_peer-88c2cb1-roe-pit-nullfix-20260809`：

- `round4/v7.13/baseline.py:43-51` 中的 `completed_month_ends`，会把**最新一行数据所在的月份**整月去掉：

  ```python
  last_available_month = pd.Timestamp(all_dates[-1]).to_period("M")
  return [pd.Timestamp(day) for day in month_ends
          if pd.Timestamp(day).to_period("M") < last_available_month]
  ```

  月末最后一个交易日收盘后，最新一行就是月末本身，于是刚结束的月份被去掉，程序退回上个月。

- `round4/v7.13/execution.py:67-82` 中的 `latest_month_end` 本来就按**决策日期**判断，规则正确。但 `baseline.load_inputs` 先调用了 `completed_month_ends`，它拿到的列表已经少了这个月。

同一件事有三处各自判断：外层调度（按交易所日历）、选股程序内部的两层（一层按数据，一层按决策日）。规则不一致，外层的结果也没有传进去。

### 影响

- V713 的模拟盘、Base 影子以及 11 月起的实盘股票腿，都在每月**第二个**交易日执行：10/09、11/03。研究假设在哪一天执行，需要和这个对照确认（**待确认**）。
- 11/02 同一天发布的 Hydra 周期和股票腿周期都从 11/03 开始，比设计（11/02）晚一天。
- 不修的话，11/30 会再被拦一次，12 月换仓仍要等到 12/01 晚才能生成。

### 修订方向（未实施）

只在一处决定"用哪个月"：由外层调度把 as_of 日期作为参数传给选股程序，选股程序只核对数据是否覆盖到这一天、这一天是否为月末，不再自行推断。上线前需在研究回测上证明历史各月结果不变；按上面的逻辑，只有数据末尾那个月会受影响。

## 三 问题 2：2027 年起无法生成篮子

同一发布目录的 `common.py:49` 中，`load_calendar(start_year=2015, end_year=2026)` 只保留 2026 年及以前的月末，`baseline.load_inputs` 调用时没有传年份参数。推演如下：

- 12/31 月末：先遇到问题 1，被拦；2027-01-04 那次运行能生成 as_of 20261231 的篮子。
- 2027-01-29 月末：月末列表最晚只到 20261231，外层预期 20270129，**之后每天都会被拦**。

修订时要连带检查所有 `load_calendar` 调用方，以及以月末个数为键的缓存（如 `roe_cache`、`shares_cache_pit`）。

## 四 问题 3：Hydra 月度数据包

### 链路

- 生产者：客户端 `v2.3/live_client/monthly_publication.py`。它从 QMT 收集后复权行情、不复权行情、除权和交易日历，冻结后上传到 `/hydra/research/snapshots`。它判断月末用的是交易日历（`latest_completed_month_end`，第 24 行），这部分没有问题。
- 调用方：`cli.py:920` 的 `publish_server_execution`。它先发布执行行情，再**顺带**调用 `publish_monthly_data`（第 934 行），失败只记为 `WAITING_RESEARCH_UPLOAD`（第 939 行），任务本身不报错。
- 计划任务：旧任务 `Hydra-Live-ExecutionData-1530`（`Register-HydraLiveOperationsTasks.ps1:11`，阶段 `publish-execution`）。
- 消费者：服务器 `qmt-hydra-monthly.timer`（交易日 15:00–18:50，每 10 分钟一次），收到包后计算权重，生成 OMS 待审批周期。

### 断点

- `Register-HydraOmsTasks.ps1` 带 `-DisableLegacyTasks` 运行时，会停用全部 `Hydra-Live-*` 任务（第 49、88 行）。
- 新注册的 9 个 OMS 任务（第 36–47 行：pre、buy、cancel、sell、eod、upload-spool、intraday）都不上传月度包。

### 证据

- 生产库 `hydra_monthly_cycles` 只有一条记录：as_of 20261030，收到时间 2026-11-02 16:16（人工补传）。在此之前没有收到过任何一期月度包。10 月的 Hydra 周期（C1）是 10/07 22:03 发布并审批的，不经过这条链路。
- `qmt-hydra-monthly` 从 10/30 15:00 到 11/02 16:14 共运行 32 次，每次都是 `IDLE_NO_NEW_MONTHLY_INPUT`。
- 9/30 那期为什么也没进库（旧任务当时是否在跑、是否上传失败），需要查看客户端日志 `hydra-live-publish-execution.log`（**待核实**）。

### 同一批旧任务里的其他附带职责

| 旧任务 | 实际承担的工作 | 停用后 |
|---|---|---|
| `Hydra-Live-ExecutionData-1530` | 执行行情发布 + 月度包上传 | 执行行情已不需要（OMS 用快照报价）；月度包**无人接替** |
| `Hydra-Live-MarketBackup-1530` | QMT 行情备份上传 | 服务器 `data/live_qmt_backups` 最后一期是 **20260930**；是否还有人依赖它，**待核实** |

这说明问题不在"该由哪一端负责"，而在于这些职责从来没有被单独列出来。原始数据只能在本地 QMT 取得，采集和上传放在本地端是合理的。但它应该是一个独立任务，有自己的回执和告警；服务器在月末收盘后到某个时点还没收到包，也应主动告警。

## 五 问题 4：生产代码版本

11/02 晚的生产状态（`systemctl cat`、发布目录 `RELEASE_MANIFEST.json`）：

| 服务 | 代码 | 在 GitHub 上 |
|---|---|---|
| `qmt-server`（接口）、`qmt-hydra-monthly` | `releases/oms-stockleg-reserve-b175e55`（11/02 19:45） | **否** |
| （上一层）| `releases/hydra-monthly-oms-7382630`（11/02 16:12，父提交 `3d2fbf4`） | **否** |
| `qmt-v713-stock-valuation`、`qmt-v713-synthetic` | `releases/oms-v713-initial-cash-3d2fbf4c` | 是 |
| `qmt-live-performance` | 工作区 `/opt/qmt-server`，`b688b6c`（9/27） | 是 |
| `qmt-shadow-ledger` | `releases/shadow-close-20261002` | 未核对 |
| `qmt-v713-base`（选股） | 策略 `88c2cb1` + 工作区 `b688b6c` 的服务器脚本 | 是 |

- `7382630` 相对 `3d2fbf4` 新增了 `app/oms/hydra_monthly_bridge.py`、`scripts/run_hydra_monthly_oms.py`、`scripts/rehearse_hydra_monthly_oms.py` 及测试。
- `b175e55` 相对 `7382630` 修改了 `app/oms/cycles.py`、`app/oms/planner.py`、`scripts/oms_ops.py`，并新增 `test_oms_stockleg_cash_reserve.py`。
- 这两个发布目录还带有客户端文件 `monthly_data_agent.py`、`monthly_freeze.py`，仓库里没有。
- 发布目录的文件是 CRLF 换行，说明是从 Windows 工作区打包的。
- `qmt-server.service` 当前叠了 6 个覆盖配置（`20-` 到 `70-kobold-cash-reserve-20261102.conf`），每层都覆盖 `ExecStart`。

需要先把 `7382630` 和 `b175e55` 从原机器推到 GitHub，否则统一修订没有可靠的起点。

## 六 问题 5：V713 篮子中的 Hydra 半边

`run_v713_weekly.sh:17` 固定读取 `/opt/qmt-refresh/releases/permenant_portfolio-66985a1/v48/output_forward_rate_bond/v713_hydra_latest.parquet`。这个文件的 as_of 是 **20260630**（7/25 生成，9 个 ETF，其中 511160 占 75%）。10/09、11/02、11/03 三次篮子记录的 `hydra_weights_sha256` 都是同一个值（`8435571e…`）。

- 实盘股票腿只取篮子中的股票部分，合成账本的 Hydra 腿用实盘 Hydra 实例的净值，二者都不受影响。
- 模拟盘 V713 和 Base 影子的 Hydra 半边与实盘 Hydra 不是同一组权重。拿它们当参照时，需要注意这一点。

## 七 11/02 晚的生产状态

| 项目 | 内容 |
|---|---|
| Hydra C2 | 信号 20261030，16:20 发布，16:29 审批，交易日 11/03–11/06；11/03 计划卖出 2 笔 |
| 股票腿 C3（`live_v713_stockleg`） | 信号 20261030，T1_5050，首期现金建仓；19:48 发布，19:55 审批，交易日 11/03–11/10；11/03 计划买入 49 笔 |
| V713 状态记录 | 信号 20261030，决策日 20261103 |
| 模拟盘 V713 | 11/03 生成 70 笔，10 月剩余 55 笔已过期 |

同事 15:29 的状态汇总写的是"没有 11 月新周期"，那是两个周期发布之前的状态。

## 八 统一修订前建议先定下来的事

1. **职责表。**每个生产任务写清楚：哪一端负责、入口、代码版本、输入、输出、回执、告警、失败后怎么恢复。旧任务每一项附带的输出都要有对应的接替者。
2. **部署规则。**只部署 GitHub 上已推送的提交；一个服务对应一个发布目录；用一份清单记录每个服务当前的版本。
3. **月末规则。**由谁决定 as_of 和执行日；研究假设第一个还是第二个交易日执行。
4. **验收。**11/27（周五）之前，用真实月末数据在库副本上跑通两个策略的全流程：数据 → 月度包 / 篮子 → 计划 → 审批预览。不能只验证"给定输入后的计划"。

## 附 证据来源

- V713 运行日志：`/opt/qmt-refresh/logs/v713_weekly_20260930_*.log`、`_20261008_1730`、`_20261030_*`、`_20261102_*`
- Hydra 月度任务：`journalctl -u qmt-hydra-monthly --since 2026-10-30`
- 生产库只读查询：`hydra_monthly_cycles`、`oms_cycles`、`oms_target_versions`、`oms_orders`、`v713_state_records`、`instance_state`
- 发布目录比对：`diff -rq --strip-trailing-cr`，比对 `oms-v713-initial-cash-3d2fbf4c`、`hydra-monthly-oms-7382630`、`oms-stockleg-reserve-b175e55`
- 本仓库代码：`0350ad2`（`origin/codex/v713-live-readiness-20261015`）
