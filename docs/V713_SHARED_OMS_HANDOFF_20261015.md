# V713 实盘准备 Handoff

日期：2026-10-15。方案：**沿用 Hydra 的物理券商账户，Hydra / V713 使用独立 attributed 账本，由一台指定 Windows 执行器协调。**

接收人：负责 Windows / QMT 的同事和 server 维护人。工程接口人：本轮开发侧。策略及资金规则由策略负责人确认。

**接手后先做现场取证和只读验收，回传结果给开发侧。** 本侧完成代码、合成联调及工程回归；尚未完成目标 Windows 原生验收，也没有新增一份按本次 V713 实盘 policy 运行的历史收益回测。生产服务、账本、资金分配、交易开关和任务未由本次改动。

## 交付内容和代码入口

| 材料 | 用途 |
|---|---|
| 本 Handoff | 交付范围、接手顺序、责任和通过条件 |
| [工程验证报告](../reports/v713_live_readiness_20261015/REPORT.md) | 本地 / server 回归、联调、故障恢复和 schema 副本证据 |
| [回测与验证证据报告](../reports/v713_live_readiness_20261015/BACKTEST_AND_VALIDATION.md) | V713 历史研究与模拟账本的证据边界、仍需补做的历史重放 |
| [Windows 操作和回传步骤](V713_SHARED_OMS_WINDOWS_ACCEPTANCE_20261015.md) | GitHub 拉代码、现有脚本部署、现场只读检查和回传文件 |
| [原工程评估与 T00–T15 清单](V713_LIVE_ENGINEERING_ASSESSMENT_20261015.md) | 最初工程估算；实施状态以本 Handoff 为准 |

GitHub 是主交付渠道：[草稿 PR #3](https://github.com/FionaMMC/MaolaoyeServer/pull/3)，分支 `codex/v713-live-readiness-20261015`。已测功能基线为 **`bcdfd166faf13281c535d514c73d809570231ac9`**，后续文档和回执更新不改变这份功能代码。按 PR 最新 Handoff 接手，固定该提交运行验收。

server 与 Windows 各自从 GitHub 获取固定版本，按原有 release / 安装脚本流程更新。ZIP 只是可选离线源码归档，不是接手前提。更新 server 后端不会同步更新 Windows 执行器，双端要分别核对实际加载版本。

## 明确的下一步

| 顺序 / 负责人 | 具体动作 | 必须交回的产物 | 通过条件及后续 |
|---|---|---|---|
| N01 / Windows 同事；server 维护人配合 | 先记录当前 active release、实际脚本路径、任务、QMT/Python 版本和账户指纹；单独核查报告三笔历史单及旧缓存责任 | `before` 检查 JSON；三笔单的脱敏终局、剩余责任和临时修改说明；检查器提前退出时改交失败阶段、版本与实际文件哈希摘要 | 当前运行版本与订单责任可解释；如有未决 UNKNOWN、错绑定或旧 journal 无绑定证据，先交开发侧处理，不覆盖现场 |
| N02 / Windows 同事 | 从 GitHub 准备固定提交的独立干净目录；在维护窗口按原安装器备份并更新客户端；保持调度停用、交易关闭，运行本地离线及 QMT 只读检查 | `local` / `qmt-readonly` 约定回执、版本与哈希、行情字段 fixture、冻结现金字段可用性、备份清单摘要 | PowerShell 原生检查通过，绑定正确，blockers 为空；实际零冻结须能观测，不能把缺字段填 0。验收失败将原始错误类型和阶段交开发侧，不清库取得 PASS |
| N03 / 开发侧 | 收到 N01–N02 回执后核对真实字段和现有代码差异；修复不兼容项并重跑受影响测试；旧 journal 尚无绑定证据时，由开发侧据旧订单及账户身份准备迁移步骤，不让对方改库消除 blocker；server 维护人在隔离候选服务上核对 v2 协议及实际只读快照，`QMT_OMS_LIVE_ENABLED=false` | 现场问题处理表、修复 / 迁移提交及复测回执；双端版本和协议核对结果 | 所有字段 / 状态问题已解释，账户核对通过；仍不发布或审批真实目标 |
| N04 / 策略负责人提供输入；开发侧实施 | 明确拟用 V713 policy、真实证券 / 费用规则、选股 AUM 与拟投入资金；开发侧在远程隔离目录完成同一 Base 目标的执行回放及账本桥接，旧研究 / A/B/C/D 扩展另列跟踪 | 规则及输入 manifest；V713 执行回放报告，含覆盖率、净收益、回撤、成本、整手偏离、成交 / 未成原因和敏感性；缺证项逐项列出 | 同目标、冻结 policy 和账本可复现；缺价、成本及容量不被隐藏；策略负责人根据已明确的目标评估结论，不以工程测试代替策略收益证据 |
| N05 / 开发侧准备；策略 / 资金负责人确认；双端维护人执行 | 形成生产 release / 停止恢复方案、真实期初 / 划拨清单和首轮目标 dry-run；核对全部差异后再进入实际部署与启用窗口 | 固定提交、双端发布步骤、资金归属回执、逐股计划及差异、负责人和观察安排 | N01–N04 问题已闭合，资金不重复认领；随后覆盖一轮真实卖 / 买 / 余量终局和日终账务，才算首轮实盘验收 |

N01 可先做；N02 的代码切换安排在维护窗口。N03 的兼容性修复和 N04 的研究由开发侧继续负责，不要求 Windows 同事替开发侧写回测。下一次需要对方返回的是 **N01–N02 的现场回执**，不是下载一个包后自行开启交易。

## 实现范围及验收边界

| 原任务 | 本侧已实现、验证的能力 | 目标环境仍需验收 / 操作输入 |
|---|---|---|
| T00 版本与事故核查 | GitHub 同步；只读核对 server 实际 release；Windows 版本、任务、绑定、journal 检查脚本 | Windows 当前实际包/临时改动；报告三笔历史单的真实终局与缓存是否仍有责任 |
| T01 次日误停牌 | 不再用前日 is_trading 过滤次日买入；状态缺失保留 None；收盘价与盘口分离；不复权价格和来源时间校验 | QMT 收盘空盘口、tick 时间及当日缓存日线样例；真实停牌/拒单原因回读 |
| T02 提交闸门 | 自动/人工单共用风控；全局开关、策略暂停、HOLD、账户绑定、限额和短期租约 | 实际 env / HOLD 路径、断网后的许可失效 |
| T03–T04 目标与全集 | V713 篮子验证、来源哈希、月份幂等、日期分离；新目标 + 原持仓退出 + 未决责任 | 在服务器准备当期原篮子、真实不复权锚及足够交易日历，保留 dry-run 清单 |
| T05–T06 证券及冻结规则 | 显式证券规格、股票/ETF tick/lot/费用/T+1；按周期冻结 policy；当前券商价格规则交叉检查 | 首期证券池、板块权限、真实费率、有效价格范围及经确认的 V713 policy；不得把合成测试参数当实盘规则 |
| T07–T09 多策略执行与资源 | v2 计划哈希、动态全集、账户级队列、策略独立目标/现金/持仓/可卖量；持久 UNKNOWN 预留；真实冻结现金；账户核对失败阻断 | Windows SDK 的 frozen_cash、PriceTick、涨跌停及证券字段；一个物理账户只允许一个有写能力的执行器 |
| T10–T11 费用与期初 | 冻结费用规格、累计费用结算、更正回执；不可变期初回执、原子资金划拨、并发与未决单保护 | 用户确认首期金额/资金来源/是否认领既有真实持仓；使用新鲜只读快照形成期初清单 |
| T12 估值与公司行动 | 不复权实体文件哈希证据；缺证不造 NAV；跨策略分红身份隔离；税/送转等问题单、暂停、复核更正 | 真实行情元数据、到账/费用凭证、实际公司行动样本；复杂动作走复核流程 |
| T13–T14 恢复与演练 | 两个真实 Python 组件经 FastAPI 合成券商完整卖/买窗口；响应丢失/重启无重单；双库 SQLite 一致性备份、spool/HOLD 保留；Windows 只读验收包 | Windows PowerShell 5.1 原生执行、QMT 只读连接、任务路径/锁/重启恢复；本机没有 PowerShell，静态检查不替代原生运行 |
| T15 发布与首轮实盘 | GitHub 固定提交、校验清单、停止/恢复纪律、分阶段检查单 | 对方验收通过后制定具体生产发布与首期资金/订单清单，再进行启用；首轮真实成交及日终核账属于实盘验收 |

首版不进行策略间内部成交净额化，也不自动多机接管。两个策略可以买卖同一证券，但各自订单、成交与账本独立。V713 的当日 T+1 买入不能借 Hydra 昨仓获得策略可卖量。

## 从 GitHub 固定版本重复工程验证

在独立的干净 Git checkout 中确认 `git rev-parse HEAD` 为上述已测功能基线，再从仓库根目录执行以下命令。Python 应包含项目依赖，测试环境还需 pytest、pytest-asyncio、httpx；不得加载生产 env 运行合成演练。以下是工程验证，不是策略收益回测。

```sh
export PYTHONDONTWRITEBYTECODE=1
export QMT_SCHEDULER_ENABLED=false
export PYTHONPATH="$PWD/v2.3/server:$PWD/v2.3"
python -m pytest -c v2.3/server/pyproject.toml -p no:cacheprovider -o addopts='' -q v2.3/server/tests
python -m pytest -p no:cacheprovider -o addopts='' -q v2.3/live_client/tests
python tools/rehearse_v713_shared_oms.py --output /tmp/v713-synthetic-receipt.json
python -m live_client.oms_offline_acceptance
```

`tools/verify_v713_schema_copy.py` 可在 qmt 上将生产 SQLite 以 `mode=ro + query_only` 打开，一致性备份到**新的远程隔离目录**，只在副本执行建表和映射检查。它核验原表结构及行数不变、quick_check 正常，只输出汇总；副本不得下载本地。调用方须提供 `--source-db`、`--copy-db` 和 `--receipt`。当前新增 schema 为 accounting receipts 表及索引，不对既有生产表增列或重写数据。

## 服务端部署候选的验收顺序

1. **先核对 Git 提交，再验副本。** 从 GitHub 获取固定版本，在新的隔离 checkout / research-runs 目录使用现有服务 Python 运行回归、合成演练和 schema-copy 检查。不要在生产 `/opt/qmt-server` 工作区直接切分支，或加载生产 env 启动合成演练。已有本轮回执可供比对，仅在版本、环境或问题变化时重跑受影响验证。
2. **记录实际运行基线。** 本次只读确认主服务为 `releases/oms-1d0dd3119ac7cb70ea24356ae66f1490691388e2`，生产有一个 attributed live 实例，尚无 V713 live 实例。主 checkout、影子 worker 和运行 release 并非同一目录；发布时以服务实际入口为准。
3. **保持新交易关闭。** 正式部署候选需要单独的具体操作窗口和回退方案；先停新单，留事实采集、明确的撤单及恢复通路。候选先以 `QMT_OMS_LIVE_ENABLED=false` 核验接口和实际快照，不发布或审批目标，不初始化资金。
4. **双端协议核对。** `QMT_LIVE_QMT_ACCOUNT_SHA256` 使用现有账户指纹；Windows `shared_oms` 与同一 canonical account alias 配对。共享账户的执行许可只走 `/oms/live/plans` v2；旧 `/plan` 不发放共享执行许可。操作员密钥不发客户端，执行密钥不用于操作员入口。
5. **核对真实快照。** 账户现金、实际冻结现金、持仓、可卖量、委托/成交身份和行情必须可解释。共享账户存在未结买单而 `frozen_cash` 缺失时明确阻断，不能填 0 绕过。最新一次快照必须完成并通过账户核对，才可发许可。
6. **创建真实期初。** 新实例必须 `ledger_mode=attributed`，并显式指定 `allocated_cash` 与 `allocated_positions`。`POST /accounts/initialize-from-qmt` 仅在期初闸门开启且账户指纹匹配时生效；快照须在 5 分钟内、没有未终局订单、现有归属总和不超物理账户。若钱原属 Hydra，先以零/未分配资源初始化可用实例，再走经确认的 `/accounts/capital-transfers` 原子划拨，不能两边同时认领。不可复制 Shadow_Base 模拟收益或改写旧期初回执。
7. **先做目标 dry-run。** 输入在 qmt 准备并保留，不下载原始数据。审核整手误差、旧股退出、最低费用占比、无法配置证券、月份/节假日和窗口；参数由策略/账户负责人确认。`--apply` 创建 PENDING_APPROVAL 周期，`approve --yes` 才使其 ACTIVE；这两步不属于本次已执行操作。

```sh
python -m scripts.oms_ops publish-v713 \
  --instance live_v713_base --account hydra-live \
  --basket /remote/reviewed/v713_target_latest.parquet \
  --policy /remote/reviewed/v713-policy.json \
  --closes /remote/reviewed/unadjusted-closes.json \
  --calendar /remote/reviewed/calendar.json
```

上述路径为占位，缺文件或规则不全必须停止。实盘不得使用 `--now` 伪造发布时间。发布规则显式要求 `policy_id`（`V713_` 开头）、`strategy_id=v713`、lot、size_factor、default_buy_bps、buy_bps、sell_schedule、window、reserve、instruments、require_instrument_specs=true、execution_model、execution_windows、submit_at。每个证券须填全部 12 项规格：asset_type、price_tick、buy_lot、min_buy_quantity、sell_lot、odd_lot_sell_all、t_plus_one、commission_rate、min_commission、stamp_duty_sell、transfer_fee_rate、buy_reserve_rate。未取得真实规则不能依靠 Hydra 默认值启动股票交易。

## 必须回传的外部验收结果

验收回执至少给出：完整 Git source_commit、Windows 当前 active release 及其代码哈希、Python/QMT 版本、实际加载脚本路径、任务/时区/锁、脱敏账户指纹、双库检查及备份清单、离线 gate、只读行情与冻结现金字段结果、未决 UNKNOWN 数量及解释、重启恢复结果。敏感账户/行情明细留在目标机器或 qmt，回传脱敏摘要和必要字段样本。

通过 Windows 离线与只读验收后，还需确认四个实盘输入：**V713 policy、首期资金与归属、单一执行器、首轮目标及订单差异清单。** 只有这些形成具体可核查方案，才进入真实发布、启用与首轮成交核账。

## 停止、恢复和回滚

- 出现账户差异、错账户/哈希、UNKNOWN 无证据、公司行动未核等情况，停止新单并保留日志；账户核对失败自动阻断许可。策略控制入口 `/oms/live/control` 需要实例、操作人和原因。
- 安装器要求相关任务事先停止/停用，备份 configured state DB 与 OMS journal、spool、HOLD 和绑定，不自动重新启用任务。`Register-HydraOmsTasks.ps1 -StageOnly` 仅登记停用任务。
- 下单后代码可以退回，数据库和 journal 不可回到报单前快照。先以券商事实确认在途及成交，再让能识别这些责任的版本接管；保留原 UNKNOWN 意图，通过服务端终态证明释放资源，不删除未知记录重试。
- 报告中的三笔历史单单独核查真实终局。新修复不会更改旧计划或 Windows 缓存；不要重放过期计划。若仍有真实责任，另做有版本和差异清单的恢复方案。

## 可选离线归档

如目标机器暂时不能访问 GitHub，可以使用此前固定功能基线的源码 tar / Windows ZIP，并核对 `RELEASE_MANIFEST.json` 和 `build_receipt.json` 的提交及哈希。它们用于离线复现，不能替代本 Handoff、回测 / 验证报告和现场回执。GitHub 主流程不要求先取得或解析这些附件；后续文档更新以 PR 最新内容为准。
