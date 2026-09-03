"""Code-grounded catalog rendered by the server architecture review page.

This is deliberately data, not HTML. Stable item ids are also the foreign keys
used by the collaborative notes API.
"""
from __future__ import annotations


REVIEW_SESSION_ID = "server-architecture-2026-09"

REVIEW_CATALOG = {
    "meta": {
        "title": "Server 业务流程与风控闸门审阅",
        "subtitle": "把计算、执行、账本、影子与监控分开讲清；逐项判断风险现在是否可能发生。",
        "as_of": "2026-09-03",
        "basis": "基于当前仓库代码、strategies.yaml、本地 SQLite 状态与 V7.13 审计材料；生产环境变量和 QMT 在线事实需会上补证。",
    },
    "principles": [
        "研究/策略只产出意图；订单风控决定能否进入执行。",
        "真实成交是账本变化的唯一交易来源；监控不得反向修改账本。",
        "影子实例物理上不能触达订单表、QMT 账户或成交结算。",
        "paper/live 由凭据确定，不接受请求方自由切换。",
        "未知数据必须显示 missing 或阻断，不能伪装成健康的 0。",
    ],
    "current_state": [
        {"label": "配置实例", "value": "3 常规 + 5 影子", "detail": "paper_v20h / paper_v53 / paper_v79 开启订单；5 个 shadow 全部 orders_disabled。"},
        {"label": "常规执行域", "value": "paper · shared_ledger", "detail": "三条常规策略共享账户总账，实例靠成交血缘维护虚拟子账本。"},
        {"label": "影子输入", "value": "1 / 5 target 在位", "detail": "本地当前只有 Hydra target；其余四个实例应被逐实例 blocked，不应影响主链。"},
        {"label": "本地数据库", "value": "仅见 paper_v20h", "detail": "当前 workspace SQLite 未见订单/成交；这不是生产运行证据。"},
        {"label": "生产事实", "value": "会上补证", "detail": "live 五开关、密钥分离、QMT 连通、现金/持仓一致性无法由仓库快照证明。"},
    ],
    "interfaces": [
        {"group": "健康与页面", "paths": "GET /healthz, /readyz, /dashboard, /dashboard/review", "owner": "Health / UI", "note": "静态页面公开；业务批注和所有事实 API 仍需 Bearer。"},
        {"group": "行情入口", "paths": "POST /market-data; POST /admin/strategies/{id}/upload-data", "owner": "Ingest / DataUpload", "note": "canonical 行情与策略私有文件是两种不同契约。"},
        {"group": "常规执行", "paths": "POST /admin/run-pipeline; GET /orders; POST /trade-result", "owner": "Pipeline / Orders / Settlement", "note": "生成、领取、成交三个事务边界，不应由单个路由跨写。"},
        {"group": "账户事实", "paths": "POST /cash-flows; POST /accounts/initialize-from-qmt; GET /admin/reconciliation/*", "owner": "CashFlow / Initialize / Reconcile", "note": "现金流不可变；QMT 只提供真实账户事实，不能直接分配策略归属。"},
        {"group": "Hydra 专用", "paths": "POST /hydra/targets/stage, /retry, /attempts/close, /trigger; POST /hydra/live-qmt-backup", "owner": "HydraRelay / HydraData", "note": "专用 target/residual 生命周期，不能与常规 pipeline 的完成状态混用。"},
        {"group": "运维查询", "paths": "GET /admin/live-snapshot, /alerts, /metrics, /performance, /risk, /shadow/*", "owner": "Ops read models", "note": "只读派生面；missing 必须显式，不能反向修业务账。"},
        {"group": "会议协作", "paths": "GET /admin/architecture-review/session; POST comments; PUT decisions/{item_id}", "owner": "ArchitectureReview", "note": "只写独立会议表，不触碰订单、成交、现金、持仓或策略状态。"},
    ],
    "stores": [
        {"name": "Canonical Parquet", "facts": "stocks / indexes / etfs 日频行情", "writers": "IngestService", "consumers": "ContextBuilder、策略、估值", "boundary": "不能接收 live 诊断备份；当前写入缺原子替换/锁。"},
        {"name": "Hydra content store", "facts": "model/raw prices、actions、calendar、manifest + SHA-256", "writers": "离线 stage 工具", "consumers": "HydraRelayService", "boundary": "内容寻址、不可变；与普通 Parquet 生命周期分离。"},
        {"name": "SQLite 业务库", "facts": "signal、order、lineage、trade、instance ledger、cash journal", "writers": "Precheck/Aggregate/Settlement/CashFlow", "consumers": "执行、对账、读模型", "boundary": "真实成交是交易型账本变化来源；共享账户不得猜测归属。"},
        {"name": "SQLite shadow_*", "facts": "理论现金、持仓、调仓与 NAV 快照", "writers": "ShadowLedgerService", "consumers": "影子 dashboard / 对比", "boundary": "不得导入 Order/Trade 或绑定 QMT。"},
        {"name": "live_qmt_backups", "facts": "QMT 诊断 CSV + manifest", "writers": "专用 backup endpoint", "consumers": "人工灾备/审计", "boundary": "diagnostic_backup_only，永不成为 canonical feed。"},
        {"name": "architecture_review_*", "facts": "追加评论 + 每项共享结论", "writers": "本审阅页", "consumers": "参会者 / Markdown 纪要", "boundary": "协作元数据独立，不进入任何交易判断。"},
    ],
    "flows": [
        {
            "id": "flow-standard",
            "name": "A. 常规 EOD 策略订单管线",
            "scope": "V20H / V53 / V7.13 adapter，经统一 StrategyPipeline 生成订单",
            "steps": [
                ["A1", "行情接收", "Windows client → POST /market-data → IngestService → Parquet", "仅入库；当前接口响应里的 strategy_triggered=true 并不代表真的触发了策略。"],
                ["A2", "管线触发", "APScheduler 或 POST /admin/run-pipeline → StrategyPipeline.run", "解析执行域、日期、新鲜度、历史批次与严格实例阻断项。"],
                ["A3", "策略计算", "strategies.yaml → StrategyRunner → plugin + Context", "每实例拿自己的虚拟现金、持仓、状态、黑名单和执行 guard。"],
                ["A4", "统一订单闸门", "RawSignal → PrecheckService → raw_signals", "FAIL 仍留审计记录；只有 PASS 进入归集。"],
                ["A5", "归集与血缘", "AggregateService → orders + order_signal_map", "按执行域/账户别名/account_group/标的/方向归集，保留 signal 血缘。"],
                ["A6", "订单领取", "GET /orders?date=... → 仅 PENDING，SELL 优先", "首次领取写 fetched_at，之后默认禁止该日重算。"],
                ["A7", "成交结算", "POST /trade-result → SettlementService → trades + instance_state", "累计回报转增量，按血缘拆回虚拟子账本。"],
                ["A8", "日终读模型", "Perf / DailyRisk / Reconcile / Ops → dashboard", "绩效和监控是派生状态；对账负责发现真账与虚账差异。"],
            ],
        },
        {
            "id": "flow-hydra",
            "name": "B. Hydra 专用 target / residual 执行链",
            "scope": "不可变研究数据 + 专用 live 风控；直接创建可审计订单批次",
            "steps": [
                ["B1", "冻结数据", "stage_hydra_data.py → HydraDataStore", "模型后复权价、原始执行价、公司行动、交易日历按 SHA-256 内容寻址。"],
                ["B2", "目标入场", "POST /hydra/targets/stage → HydraRelayService", "验证 target、publisher、四流 hash、日期、白名单、停牌与账户状态。"],
                ["B3", "专用限额", "target weights → target shares → residual → HydraRiskLimits", "live 校验订单数、单笔/买卖/换手金额、价格偏移、实际现金与持仓。"],
                ["B4", "确定性批次", "target/rebalance/attempt/batch/order id → orders + lineage", "同内容重复请求返回同一批次；同月不同 target 拒绝替换。"],
                ["B5", "回报与结案", "通用 SettlementService → /hydra/attempts/close", "QMT 现金/持仓必须和虚拟账本一致；否则不能 COMPLETE，只能形成 residual 后重试。"],
            ],
        },
        {
            "id": "flow-shadow",
            "name": "C. 纯影子账本",
            "scope": "只做理论持仓/NAV，不进入任何订单或成交路径",
            "steps": [
                ["C1", "影子目标", "外部 target parquet + provenance sidecar", "八字段契约、hash、版本、publisher、allowlist、时效。"],
                ["C2", "独立调度", "ShadowLedgerService.run_all", "逐实例失败隔离；配置层拒绝 QMT account 和 orders_enabled=true。"],
                ["C3", "理论调仓", "目标权重 → 卖先买后 → 影子现金/持仓", "按 lot、费用和可用现金做理论撮合，不创建 RawSignal/Order/Trade。"],
                ["C4", "影子读模型", "shadow_* tables → 风险快照 / dashboard", "只参与对比和研究，不可被客户端领取。"],
            ],
        },
        {
            "id": "flow-ops",
            "name": "D. 运维与只读监控链",
            "scope": "从业务事实派生视图，不承担订单正确性",
            "steps": [
                ["D1", "事实源", "SQLite + Parquet", "订单、成交、血缘、虚拟账本、行情与影子状态。"],
                ["D2", "读模型", "Perf / DailyRisk / Metrics / OpsMonitor", "NAV、收益、风险、执行质量、数据新鲜度与异常。"],
                ["D3", "告警", "AlertEngine → /admin/alerts", "目前请求时计算，缺少持久 sink、确认/关闭闭环与外部值班渠道。"],
                ["D4", "展示", "/dashboard 与 /dashboard/review", "页面不应成为业务写入入口；本审阅页只写会议批注与结论。"],
            ],
        },
    ],
    "boundaries": [
        {"id": "boundary-api", "layer": "API 路由", "owns": "HTTP、鉴权依赖、请求/响应映射", "must_not": "直接实现策略、账本或跨表业务规则", "current": "大体遵守；canary 路由直接写 Order，是一个例外。", "source": "app/api/*.py"},
        {"id": "boundary-schema", "layer": "Schema", "owns": "格式、类型、字段级不变量", "must_not": "查询 DB 或决定业务状态", "current": "Hydra 契约较严；普通 market/trade schema 的业务不变量仍不完整。", "source": "app/schemas/*.py"},
        {"id": "boundary-service", "layer": "Service", "owns": "单一业务能力与事务边界", "must_not": "依赖 FastAPI 或前端展示", "current": "常规 service 清楚；ReconcileService 同时承载 legacy/白名单/shared-ledger 三套语义。", "source": "app/services/*.py"},
        {"id": "boundary-orchestrator", "layer": "编排", "owns": "步骤顺序、跨服务 fail-fast、批次生命周期", "must_not": "重写策略算法或隐藏失败", "current": "StrategyPipeline 集中清晰；HydraRelayService 形成第二条订单编排路径。", "source": "app/scheduler/pipeline.py; app/services/hydra_relay.py"},
        {"id": "boundary-strategy", "layer": "策略插件", "owns": "目标/信号、策略私有状态与策略内过滤", "must_not": "直接写订单/成交/共享账本", "current": "遵守；但不同 adapter 对缺数据的 fail-open/fail-closed 语义不一致。", "source": "plugins/*_adapter.py; app/strategy/*"},
        {"id": "boundary-ledger", "layer": "成交与账本", "owns": "真实回报、订单血缘、现金/持仓原子更新", "must_not": "猜测 unmatched 成交归属或用监控结果改账", "current": "核心原则成立；缺血缘时仅告警、未统一标记 divergence。", "source": "app/services/settlement.py"},
        {"id": "boundary-storage", "layer": "存储", "owns": "SQLite 事务、Parquet 行情、Hydra 内容寻址", "must_not": "夹带策略选择或 UI 语义", "current": "SQLite 边界清楚；普通 Parquet 是 read-merge-write，缺文件锁和原子 replace。", "source": "app/db.py; app/storage/parquet.py; app/services/hydra_data.py"},
        {"id": "boundary-shadow", "layer": "影子域", "owns": "理论调仓和 shadow_* 表", "must_not": "导入订单/成交模型、绑定 QMT、开启 orders", "current": "代码与配置均有硬边界，是目前最干净的隔离。", "source": "app/services/shadow_ledger.py:100-108"},
        {"id": "boundary-ops", "layer": "监控读模型", "owns": "只读聚合、missing 语义、告警线索", "must_not": "替代前置风控或宣称未采集指标健康", "current": "missing 语义已实现；告警仍无持久闭环。", "source": "app/services/ops_monitor.py; app/services/alerts.py"},
    ],
    "risks": [
        # Configuration / identity
        {"id":"cfg-01","phase":"身份与配置","name":"paper/live 密钥不得相同","priority":"P0","type":"hard","threat":"客户端误连后静默跨执行域，模拟单变实盘单或反向污染。","control":"Settings 启动校验拒绝相同 paper/live/trigger/backup 密钥。","response":"配置加载失败，服务不启动。","likelihood":"conditional","now":"代码能拦配置错误；生产实际密钥是否分离需会上核对。","residual":"密钥泄露、轮换和最小权限仍依赖部署流程。","source":"app/settings.py:104-129"},
        {"id":"cfg-02","phase":"身份与配置","name":"Bearer token 恒时比较与未配置拒绝","priority":"P0","type":"hard","threat":"未授权调用订单、成交、管理接口。","control":"未配置任何 key、缺 Bearer 或不匹配均拒绝；使用 hmac.compare_digest。","response":"HTTP 401。","likelihood":"possible","now":"只要服务可被网络访问就持续存在；代码已前置阻断。","residual":"/dashboard 静态 HTML 与 health 路由公开；网络边界仍要靠 nginx/安全组。","source":"app/auth.py:37-109"},
        {"id":"cfg-03","phase":"身份与配置","name":"专用 trigger / backup token 只能访问精确路径","priority":"P0","type":"hard","threat":"窄权限 token 被拿去领取订单、写成交或调用 admin。","control":"按 request.url.path 精确绑定两个专用 token。","response":"HTTP 403。","likelihood":"possible","now":"live 自动化一启用就有现实意义；代码已阻断越权。","residual":"路径重命名或代理重写需要回归测试。","source":"app/auth.py:52-68"},
        {"id":"cfg-04","phase":"身份与配置","name":"live API 面与 account_alias allowlist","priority":"P0","type":"hard","threat":"live token 横向访问其他账户或未经分域审计的新路由。","control":"live 只允许显式 exact path，并由 AuthContext 校验 account_alias。","response":"HTTP 403。","likelihood":"possible","now":"多账户/多客户端时可能发生；当前代码已 fail-closed。","residual":"普通 paper legacy 空 allowlist 仍表示不限账户别名。","source":"app/auth.py:70-108"},
        {"id":"cfg-05","phase":"身份与配置","name":"live 五类写动作独立开关且默认关闭","priority":"P0","type":"hard","threat":"只因配置了 live token 就意外生成/领取实盘单、入现金流、初始化或跑 canary。","control":"generation/delivery/cash-flow/init/canary 五个布尔闸门默认 false。","response":"跳过或 HTTP 423。","likelihood":"unknown","now":"仓库默认安全；生产环境变量不可从本地证明，必须会上逐项截图确认。","residual":"开关打开后仍需额度、账户与现场证据。","source":"app/settings.py:39-48"},

        # Data ingress and provenance
        {"id":"dat-01","phase":"数据入口","name":"行情请求结构与日期格式校验","priority":"P0","type":"gap","threat":"负价格、NaN/Inf、OHLC 关系错误或非法代码进入普通行情仓库。","control":"目前只校验字段类型和 YYYYMMDD；普通行情没有正值、finite、代码格式与 OHLC 关系校验。","response":"部分坏数据会被 Pydantic 拒绝，业务上错误但类型合法的数据可能入库。","likelihood":"possible","now":"QMT 正常源通常低概率，但手工/mock/上游异常时完全可能。","residual":"这是当前暴露，HydraDataStore 的严格校验尚未复用到普通 ingest。","source":"app/schemas/market_data.py; app/services/ingest.py"},
        {"id":"dat-02","phase":"数据入口","name":"空行情包拒绝","priority":"P1","type":"hard","threat":"上游任务空跑却被当成成功，后续策略基于缺失数据。","control":"stocks/indexes/etfs 三类同时为空时抛 EMPTY_DATA。","response":"业务错误，不写入。","likelihood":"possible","now":"采集器异常时可能；代码已阻断全空，但不校验某个关键 universe 是否完整。","residual":"部分缺失包仍会成功。","source":"app/services/ingest.py:25-31"},
        {"id":"dat-03","phase":"数据入口","name":"普通行情按 trade_date 去重","priority":"P1","type":"hard","threat":"网络重试导致同日重复行，污染序列。","control":"Parquet append 先到先得；整包无新增时返回 DUPLICATE_DATE。","response":"重复行不追加。","likelihood":"observed","now":"重试是日常情况；当前可防重复。","residual":"同日内容冲突不会报警，旧值静默保留，纠错包无法生效。","source":"app/storage/parquet.py:29-55; app/services/ingest.py:38-44"},
        {"id":"dat-04","phase":"数据入口","name":"普通 Parquet 写入并发与原子性","priority":"P0","type":"gap","threat":"两个 worker 同时 read-merge-write 造成丢更新，或进程中断留下半写文件。","control":"当前直接 merged.to_parquet，无文件锁、临时文件 + os.replace。","response":"没有专门阻断或恢复。","likelihood":"conditional","now":"单 worker/单上传器时低；多 worker、重试并发或人工同时上传时可能。","residual":"需决定锁、单写者或迁移到事务型存储。","source":"app/storage/parquet.py:29-55"},
        {"id":"dat-05","phase":"数据入口","name":"策略私有文件名白名单与 Parquet 可解析","priority":"P1","type":"hard","threat":"路径穿越、任意文件覆盖或明显损坏文件进入 plugin 数据目录。","control":"strategy 注册表 + data_files 精确白名单 + .parquet + metadata parse。","response":"BAD_REQUEST，不写文件。","likelihood":"possible","now":"外部 weekly/monthly publisher 持续使用；基础边界有效。","residual":"只读 metadata 不验证业务 schema/hash/日期，且 target_path.write_bytes 非原子覆盖。","source":"app/services/data_upload.py:23-74"},
        {"id":"dat-06","phase":"数据入口","name":"V20H 资源按 mtime 自动失效缓存","priority":"P0","type":"hard","threat":"长驻进程上传新预测后仍用旧内存缓存，静默下旧信号。","control":"每次加载比较文件 mtime，变更即重读。","response":"使用新文件；缺文件时策略返回空。","likelihood":"observed","now":"该事故已发生过，当前修复有效。","residual":"mtime 粒度/同时间覆盖仍可能漏；上传和读取之间没有原子交换。","source":"plugins/v20h_adapter.py:43-66"},
        {"id":"dat-07","phase":"数据入口","name":"V20H 预测时效只记录、不硬拦","priority":"P0","type":"gap","threat":"weekly 预测或 fallback close 过旧，限价与策略状态偏离当前市场。","control":"记录 lag 与 fallback warning；没有 max_pred_age 硬阈值。","response":"仍可继续产出订单。","likelihood":"possible","now":"只要预测刷新失败一天以上就可能；代码明确允许最近历史预测。","residual":"需要会上定义最大允许年龄和缺行情时是否 fail-closed。","source":"plugins/v20h_adapter.py:131-144, 377-409"},
        {"id":"dat-08","phase":"数据入口","name":"live QMT 备份与 canonical feed 物理隔离","priority":"P0","type":"isolation","threat":"灾备/诊断行情误被策略消费，改变真实订单。","control":"专用 token、独立 live_qmt_backups 路径、manifest 标 diagnostic_backup_only。","response":"只保存不可变诊断副本，不进入 ParquetStore。","likelihood":"conditional","now":"接入 live 备份后可能；当前边界清晰。","residual":"同日同 source 冲突会拒绝，但 data/manifest 两文件写入仍非一个原子事务。","source":"app/api/live_qmt_backup.py"},

        # Pipeline and strategy orchestration
        {"id":"pln-01","phase":"管线编排","name":"live 生成总开关","priority":"P0","type":"hard","threat":"通用 StrategyPipeline 被错误地以 live 域调用。","control":"run() 第一段检查 live_order_generation_enabled。","response":"返回 skipped，不产生信号/订单。","likelihood":"conditional","now":"默认关闭；生产是否开启需核对。","residual":"HydraRelayService 也有自己的 live gate，形成两处相同概念。","source":"app/scheduler/pipeline.py:113-124"},
        {"id":"pln-02","phase":"管线编排","name":"未来批次必须等当天 EOD 行情","priority":"P0","type":"hard","threat":"T 日还没收盘就用 T-1 收盘生成 T+1 live 订单。","control":"trade_date > today 时要求 probe latest >= today。","response":"skipped: market_data_not_ready_for_future_batch。","likelihood":"observed","now":"历史事故类型；生成次日单时每天都可能触发，代码已拦。","residual":"只检查单一指数探针，不能证明全部目标标的已到齐。","source":"app/scheduler/pipeline.py:126-152"},
        {"id":"pln-03","phase":"管线编排","name":"宽松行情陈旧阈值","priority":"P0","type":"hard","threat":"灾备或历史回放拿过旧行情生成订单。","control":"探针相对 trade_date 超 max_data_staleness_days 则跳过。","response":"skipped: stale_market_data。","likelihood":"possible","now":"默认阈值 5 天；长假附近和上传中断时可能。","residual":"日历天而非交易日；阈值 5 是否符合各策略需讨论。","source":"app/scheduler/pipeline.py:154-171"},
        {"id":"pln-04","phase":"管线编排","name":"行情探针缺失时当前放行","priority":"P0","type":"gap","threat":"最需要保护的“完全没有探针数据”被当作无法评估后继续运行。","control":"当前仅 warning，并返回 None 放行。","response":"策略继续。","likelihood":"possible","now":"新部署、路径错或文件丢失时可能，属于 fail-open。","residual":"应讨论普通 paper 与 live 是否都改为 fail-closed。","source":"app/scheduler/pipeline.py:78-88"},
        {"id":"pln-05","phase":"管线编排","name":"已结算批次绝不重算","priority":"P0","type":"hard","threat":"清掉已成交订单血缘、重生新 order_id，造成孤儿成交与永久 PENDING。","control":"同 valid_date 只要存在 trade 就直接拒绝，force 也不能绕过。","response":"skipped: already_settled。","likelihood":"observed","now":"事故已发生过；日内人工重跑时仍有现实可能，当前硬拦。","residual":"按日期整批阻断，局部恢复必须走人工受控流程。","source":"app/scheduler/pipeline.py:187-202"},
        {"id":"pln-06","phase":"管线编排","name":"已领取批次默认禁止重算","priority":"P0","type":"hard","threat":"客户端拿到旧 order_id 后服务端重算换 ID，回报全量 unmatched。","control":"GET /orders 写 fetched_at；管线发现后拒绝。","response":"skipped: already_fetched；仅 force 可绕。","likelihood":"observed","now":"2026-07-02 事故类型；当前默认已拦。","residual":"force 只是审计标记，不强制客户端真的重新拉取。","source":"app/scheduler/pipeline.py:204-226"},
        {"id":"pln-07","phase":"管线编排","name":"清理批次保留已结算血缘","priority":"P0","type":"hard","threat":"幂等重跑的 delete 误删 OrderSignalMap/RawSignal/Order，成交失去父链。","control":"_clear_for_date 先找 settled order 与 signal，三个表均排除保护集合。","response":"只清未结算旧批次。","likelihood":"observed","now":"与历史孤儿成交事故直接相关；修复有效。","residual":"数据库没有外键，其他脚本仍可能造孤儿。","source":"app/scheduler/pipeline.py:730-798"},
        {"id":"pln-08","phase":"管线编排","name":"陈旧未决订单终态化","priority":"P1","type":"hard","threat":"过期 PENDING/PARTIAL 永久阻塞严格实例下一次调仓。","control":"历史 PENDING→EXPIRED，PARTIAL→CANCELLED；PENDING 但已有 trade 则保留人工审查。","response":"批量终态化并记录。","likelihood":"possible","now":"回报丢失/拒单时可能；代码会在新批次前处理。","residual":"只由管线运行触发；停跑期间僵尸单仍存在。","source":"app/scheduler/pipeline.py:655-709"},
        {"id":"pln-09","phase":"管线编排","name":"严格实例在任何清理前检查阻断项","priority":"P0","type":"hard","threat":"旧单未终局、账本分叉或上次未对账时先删旧单再发现问题。","control":"requires_reconciled_rebalance 实例预查 unresolved/divergence/reconciliation status。","response":"整条管线 skipped: strict_rebalance_blocked。","likelihood":"possible","now":"V7.13 当前启用；V20H/V53 未 opt-in。","residual":"一个严格实例会阻断同域所有实例，是否应按实例隔离需讨论。","source":"app/scheduler/pipeline.py:228-254, 484-575"},
        {"id":"pln-10","phase":"管线编排","name":"策略异常按实例隔离","priority":"P1","type":"isolation","threat":"一个插件异常拖垮所有策略与日终快照。","control":"StrategyRunner 捕获每实例异常，产出空信号后继续。","response":"仅日志异常，该实例 signals=[]。","likelihood":"possible","now":"外部文件缺失/策略计算错误均可能。","residual":"空信号与合法 no-op 在管线摘要中不可区分，可能静默漏调仓。","source":"app/strategy/runner.py:41-76"},
        {"id":"pln-11","phase":"管线编排","name":"SELL 先预检并把预计收入带给 BUY","priority":"P0","type":"hard","threat":"同日换仓时 BUY 看不到 SELL 释放现金，被错误拒绝或顺序与客户端不一致。","control":"每实例 stable sort SELL first，running cash/positions 链式更新。","response":"按预计净收入做后续预检。","likelihood":"observed","now":"已修历史 Bug A；每次换仓都生效。","residual":"使用统一 0.1% fee_rate 是估算，不等于所有品种真实费用。","source":"app/scheduler/pipeline.py:312-366"},
        {"id":"pln-12","phase":"管线编排","name":"监控快照失败不回滚已落订单","priority":"P1","type":"isolation","threat":"派生 DailyRisk 计算失败，把本已成功的订单批次伪装成失败并引发重复执行。","control":"订单提交后 risk snapshot best-effort，异常只记日志。","response":"保留订单批次。","likelihood":"possible","now":"监控数据不齐时可能；隔离设计合理。","residual":"交易可在风险监控缺失时继续，必须有独立 missing 告警。","source":"app/scheduler/pipeline.py:402-425"},

        # Strategy / order intent
        {"id":"str-01","phase":"策略与信号","name":"RawSignal 基本不变量","priority":"P0","type":"gap","threat":"非法方向、非正数量/价格或 NaN/Inf/非法代码/过大偏移进入订单链。","control":"当前仅校验方向、quantity>0、reference_price>0。","response":"前三类抛异常并使该实例空信号；其余可能通过。","likelihood":"possible","now":"所有 plugin 都是内部代码，概率低但回归错误可发生。","residual":"应补 finite、代码格式、offset 范围及数量上限。","source":"app/strategy/base.py:13-28"},
        {"id":"str-02","phase":"策略与信号","name":"A 股手数、虚拟现金与可卖持仓预检","priority":"P0","type":"hard","threat":"零股/碎股买入、现金透支、卖空或非清仓碎股卖出。","control":"PrecheckService 校验 BUY 100 整手、含费现金；SELL 不超持仓且非清仓为整手。","response":"RawSignal 记 FAIL，不生成订单。","likelihood":"possible","now":"舍入、费用和策略 diff 每次调仓都可能触发。","residual":"不校验涨跌停、停牌、账户板块权限、单笔/日累计额度。","source":"app/services/precheck.py"},
        {"id":"str-03","phase":"策略与信号","name":"风险黑名单自动持久化并注入策略","priority":"P1","type":"hard","threat":"ST、退市、协议未签等历史 REJECTED 标的被反复下单。","control":"近 30 日 REJECTED 自动晋升 + 手工表，Context 提供给 adapter 过滤。","response":"目标标的被跳过或减为 0。","likelihood":"observed","now":"历史拒单已存在过；当前机制可防重复。","residual":"按 symbol 全局生效，临时拒单也可能永久/跨策略误伤；过滤掉持仓目标可能触发卖出。","source":"app/services/blacklist.py; plugins/*_adapter.py"},
        {"id":"str-04","phase":"策略与信号","name":"V53 QDII 溢价过滤并非真实溢价","priority":"P0","type":"gap","threat":"QDII 高溢价追买，或把正常上涨误判成溢价后清仓。","control":"当前用当日 close 相对过去 20 日均值近似；数据不足返回 None 放行。","response":"超过 5% 时从 target 删除。","likelihood":"possible","now":"513500/513100 会实际使用；该 proxy 无法区分净值变化与溢价。","residual":"删除 target 对已有持仓等价于发 SELL，不只是“跳过买入”；需接 IOPV 并区分买/卖。","source":"plugins/v53_adapter.py:284-331, 375-420"},
        {"id":"str-05","phase":"策略与信号","name":"V53/V7.13 流动性过滤缺数据时放行","priority":"P0","type":"gap","threat":"目标单占最近成交量过高，冲击市场或无法成交。","control":"有 volume 时要求 volume >= qty×100；volume=None 时不拦。","response":"不足时从 target 删除。","likelihood":"possible","now":"V7.13 小盘标的和 ETF 调仓均可能；缺行情时尤其危险。","residual":"同样可能把已有持仓变成 SELL；且只看一天 volume，不是 ADV/participation。","source":"plugins/v53_adapter.py:333-343; plugins/v79_relay.py:129-158"},
        {"id":"str-06","phase":"策略与信号","name":"策略 target 内容哈希与月度幂等（V7.13）","priority":"P0","type":"hard","threat":"重复发布、回滚旧月或同 allocation 不同文件导致重复调仓。","control":"schema/weight/hash/date/sleeve 校验 + basket/allocation hash + persisted cycle state。","response":"相同内容 no-op；回滚/陈旧 target 拒绝或跳过。","likelihood":"possible","now":"外部 publisher 周更，重复和重跑很现实；代码已防。","residual":"DataUploadService 覆盖文件后才由 adapter 验证，错误文件会让本次策略静默空单。","source":"plugins/v713_relay.py"},

        # Aggregation and delivery
        {"id":"exe-01","phase":"归集与领取","name":"归集键保留执行域、账户和策略组隔离","priority":"P0","type":"hard","threat":"不同域/账户/租户的同标的信号被合成一张单，成交归属错乱。","control":"key=(domain, account_alias, account_group, symbol, direction)。","response":"仅同 key 信号合并并保留映射。","likelihood":"possible","now":"共享 QMT 与多策略是当前架构事实；边界有效。","residual":"同 account_group 多实例仍会按比例拆成交，业务是否允许需保持明确。","source":"app/services/aggregate.py:66-118"},
        {"id":"exe-02","phase":"归集与领取","name":"保守限价归集与 SELL 优先领取","priority":"P1","type":"hard","threat":"合并后限价比成员策略更差，或 BUY 先于 SELL 导致资金不足。","control":"BUY 取最高限价、SELL 取最低限价；订单列表 SELL-first。","response":"生成一张可覆盖成员约束的单并按顺序交付。","likelihood":"possible","now":"多信号同组时可能；代码已处理。","residual":"保守方向提升成交概率，也可能扩大滑点；无市场级 price-band 校验。","source":"app/services/aggregate.py:94-115; app/services/orders_queue.py:64-73"},
        {"id":"exe-03","phase":"归集与领取","name":"订单与 signal mapping 同事务落库","priority":"P0","type":"hard","threat":"有可领取 Order 却没有拆账血缘。","control":"OrdersQueueService 在同一 SQLAlchemy session 写 orders 和 mappings 后一次 commit。","response":"事务失败则整批不提交。","likelihood":"possible","now":"所有通用 pipeline 订单都经过此处。","residual":"Hydra canary 绕过该 service，直接写无 mapping 的 Order。","source":"app/services/orders_queue.py:24-55; app/api/canary.py"},
        {"id":"exe-04","phase":"归集与领取","name":"live 领取独立闸门与账户过滤","priority":"P0","type":"hard","threat":"已经生成的 live 订单在未获批准时被客户端实际领取，或领取他账户订单。","control":"live_order_delivery_enabled + domain + allowed aliases + status=PENDING + valid_date。","response":"HTTP 423 或返回过滤后的订单。","likelihood":"unknown","now":"仓库默认关闭；生产开关需会上确认。","residual":"GET 即 fetched，不等于券商已 ACK；缺领取者/ACK 的持久事件。","source":"app/api/orders.py; app/services/orders_queue.py"},

        # Settlement and ledger
        {"id":"set-01","phase":"成交与账本","name":"成交回报执行域与账户双重校验","priority":"P0","type":"hard","threat":"paper fill 写进 live 账本或一个账户的回报更新另一账户。","control":"API 校验请求域，Settlement 再校验 order 域与 alias。","response":"域不一致 403；订单找不到/账户不符列为 unmatched。","likelihood":"possible","now":"双客户端并存时可能；代码已防。","residual":"paper legacy 空 alias allowlist 仍是宽范围。","source":"app/api/trade_result.py; app/services/settlement.py:107-139"},
        {"id":"set-02","phase":"成交与账本","name":"重复成交回报幂等","priority":"P0","type":"hard","threat":"网络重试或全量重推导致现金/持仓二次入账。","control":"order_id + filled_time + filled_quantity + filled_price 查重。","response":"重复整笔跳过。","likelihood":"observed","now":"历史重复结算事故；日常重试仍会发生。","residual":"数据库无唯一约束；并发两个请求可同时查不到后双写。","source":"app/services/settlement.py:141-164; app/models/trade.py"},
        {"id":"set-03","phase":"成交与账本","name":"累计回报转增量入账","priority":"P0","type":"hard","threat":"PARTIAL→FILLED 的累计数量/VWAP被当成第二笔增量，重复记账。","control":"取历史最大累计量，计算 delta_qty 与 delta_notional。","response":"只按增量更新账本；倒退累计量忽略。","likelihood":"possible","now":"部分成交场景正常会发生；代码已处理。","residual":"未校验累计 filled_quantity <= order.quantity，也未校验累计名义金额非负。","source":"app/services/settlement.py:166-208"},
        {"id":"set-04","phase":"成交与账本","name":"成交量不得超过委托量","priority":"P0","type":"gap","threat":"错误或恶意回报 quantity 大于 order.quantity，虚拟账本被过量增减。","control":"当前 schema 只要求 filled_quantity>=0，Settlement 无上限检查。","response":"可能照常写 trade 并更新账本，直到现金/持仓防线触发。","likelihood":"conditional","now":"正常 QMT 不应发生；mock、字段口径错误或回报解析 bug 时可能。","residual":"应在写 Trade 前硬拒绝并告警 overfill。","source":"app/schemas/trade_result.py; app/services/settlement.py"},
        {"id":"set-05","phase":"成交与账本","name":"最大余数法精确拆分聚合成交","priority":"P0","type":"hard","threat":"部分成交按比例拆账时整数舍入导致拆分和不等于真实成交量。","control":"largest_remainder_split 保证 sum(splits)==filled_qty。","response":"每个 signal/instance 得到整数成交量。","likelihood":"possible","now":"同组多 signal 聚合且部分成交时可能。","residual":"按原信号数量比例分，不表达券商逐子单先后；当前业务接受该口径需确认。","source":"app/services/settlement.py:36-57, 340-398"},
        {"id":"set-06","phase":"成交与账本","name":"结算时二次防穿仓/超卖","priority":"P0","type":"hard","threat":"真实账户已成交但虚拟子账本现金或持仓不足，继续强写会让账本负数。","control":"拒绝该 split 的账本更新，标 Order.bookkeeping_divergence=true。","response":"保留真实订单状态，发 critical 线索，要求人工对账。","likelihood":"possible","now":"回报重复、归属错或费用差异时可能；代码已显式暴露。","residual":"一个聚合单部分 instance 更新、部分拒绝时事务仍提交，需靠 divergence 恢复。","source":"app/services/settlement.py:393-435"},
        {"id":"set-07","phase":"成交与账本","name":"缺 mapping/signal 时不得静默结案","priority":"P0","type":"gap","threat":"真实成交被记入 trades 且订单终态，但没有任何虚拟账本变化。","control":"当前只 warning 后跳过拆账，未标 bookkeeping_divergence。","response":"订单仍会改成回报状态并 commit。","likelihood":"possible","now":"canary 当前就创建无 mapping 的 Order；人工/迁移数据也可能触发。","residual":"应定义 canary 专用账本语义，且所有缺血缘成交统一标 divergence。","source":"app/services/settlement.py:348-373; app/api/canary.py"},
        {"id":"set-08","phase":"成交与账本","name":"unmatched 只给候选、不自动猜单","priority":"P0","type":"hard","threat":"order_id 对不上时按 symbol/quantity 自动绑定错订单，污染不可逆账本。","control":"候选仅返回人工核对，不自动结算。","response":"matched_count 不增加，回报保持未入账。","likelihood":"observed","now":"历史重生 order_id 事故；安全选择正确。","residual":"真实成交在人工处理前仍未进账；需要 runbook、时限和恢复 API。","source":"app/services/settlement.py:117-139, 312-338"},

        # Reconciliation / cash / account
        {"id":"rec-01","phase":"账户与对账","name":"账户首次初始化不可覆盖","priority":"P0","type":"hard","threat":"第二次“初始化”把已有成交演进后的账本重置。","control":"instance 已存在只允许完全相同 evidence 的幂等重放。","response":"不同内容 HTTP 409。","likelihood":"possible","now":"部署/重装时可能；代码已防。","residual":"正式恢复流程尚未在此 API 中实现。","source":"app/services/account_initialization.py:45-96"},
        {"id":"rec-02","phase":"账户与对账","name":"live 初始化/对账绑定账户指纹且只读","priority":"P0","type":"hard","threat":"正确 token 操作错误的真实 QMT 账号，或用 reconcile 强制覆盖 live 台账。","control":"qmt_account_id SHA-256 匹配；live reconcile 只允许 dry_run 且 force=false。","response":"HTTP 403。","likelihood":"possible","now":"同机多账号/人工复制配置时可能；代码已防。","residual":"指纹配置本身仍需双人核对。","source":"app/api/account_initialization.py; app/api/admin_query.py:834-890"},
        {"id":"rec-03","phase":"账户与对账","name":"现金流 journal 唯一、不可变、原子入账","priority":"P0","type":"hard","threat":"分红/入出金重推两次，或同 source_event_id 内容被悄悄改写。","control":"DB 唯一约束 + 内容一致性检查；journal 与 virtual_cash 同事务。","response":"完全相同返回 already_applied；冲突 409。","likelihood":"possible","now":"分红和人工入金正常会重试；防线有效。","residual":"OTHER 类型语义较宽；evidence 内容不由 server 复算。","source":"app/models/cash_flow.py; app/services/cash_flow.py"},
        {"id":"rec-04","phase":"账户与对账","name":"现金流不得把 virtual_cash 扣成负数","priority":"P0","type":"hard","threat":"错误出金/费用调整把子账本穿仓。","control":"cash_after < 0 直接拒绝。","response":"HTTP 409，不写 journal。","likelihood":"possible","now":"人工出金或费用补记时可能。","residual":"不能证明真实 QMT 可用现金充足，只保护虚拟账。","source":"app/services/cash_flow.py:82-90"},
        {"id":"rec-05","phase":"账户与对账","name":"共享物理账户禁止单实例 apply","priority":"P0","type":"hard","threat":"整账户 QMT 快照无法区分重叠标的归属，却覆盖某一策略虚拟持仓。","control":"任一 shared_ledger 策略使该物理账户的 per-instance apply 永久拒绝，force 不可绕。","response":"ReconcileGuardTripped。","likelihood":"possible","now":"paper_v79 已配置 shared_ledger，且与 v53/v20h 共账户；风险现实存在。","residual":"v20h 仍是 legacy owned_symbols=None，整体迁移语义仍复杂。","source":"app/services/reconcile.py:89-134, 164-181"},
        {"id":"rec-06","phase":"账户与对账","name":"非共享 apply 的大批量清仓保护","priority":"P0","type":"hard","threat":"不完整 QMT 快照把大量 server 持仓当幽灵仓清掉。","control":"server_only 同时达到 3 个且 ≥34% 时默认拦截。","response":"422；确认完整后才允许 force。","likelihood":"observed","now":"V53 曾因 5/10 快照发生过；当前有保护。","residual":"阈值是经验值；force 仍可能误操作。","source":"app/services/reconcile.py:63-78, 292-331"},
        {"id":"rec-07","phase":"账户与对账","name":"Portfolio 总量对账只报警、不改账","priority":"P0","type":"monitor","threat":"多个虚拟子账本之和与 QMT 持仓/现金不一致，继续交易扩大分叉。","control":"逐 symbol 精确比较 Σvirtual_positions 与 QMT；现金检查是否足以覆盖台账现金。","response":"记录 error 并返回 mismatch，不自动修复。","likelihood":"possible","now":"共享账户的核心兜底；本地 DB 无完整 QMT 快照无法证明当前一致。","residual":"普通策略不会因总量 mismatch 自动阻断；endpoint 里的 shadow_compare 失败也被 non-fatal 忽略。","source":"app/services/reconcile.py:386-500; app/api/admin_query.py:881-896"},

        # Hydra dedicated relay
        {"id":"hyd-01","phase":"Hydra 专用链","name":"Hydra 数据四流内容寻址与不可覆盖","priority":"P0","type":"hard","threat":"研究模型价、执行原价、公司行动或日历在下单后被静默替换，失去可复现性。","control":"文件 SHA-256 目录 + manifest SHA + schema/行数/标的/价格关系校验 + 原子 replace。","response":"冲突或损坏拒绝加载/安装。","likelihood":"possible","now":"每个研究批次都会经过；防线完整。","residual":"安装当前走 CLI 而非受审计 HTTP 工作流，部署权限需控制。","source":"app/services/hydra_data.py; app/schemas/hydra_data.py"},
        {"id":"hyd-02","phase":"Hydra 专用链","name":"目标权重、日期与 basket hash 契约","priority":"P0","type":"hard","threat":"重复代码、权重不为 1、日期倒置或 payload 被改。","control":"Pydantic model validators + canonical hydra_basket_hash。","response":"请求校验失败。","likelihood":"possible","now":"外部 publisher 输出错误时可能；代码已拦。","residual":"research_input_hashes 只校验形状，不逐个加载核实内容。","source":"app/schemas/hydra_relay.py:12-84"},
        {"id":"hyd-03","phase":"Hydra 专用链","name":"固定 9 ETF 与 publisher commit 双 allowlist","priority":"P0","type":"hard","threat":"未经批准的证券或研究代码版本进入 live 执行。","control":"Settings 要求 ETF 集合恰好匹配批准清单；stage 再校验 target 与 commit。","response":"启动失败或 BAD_REQUEST。","likelihood":"possible","now":"上游策略升级/误发标的时可能；防线有效。","residual":"批准 commit 列表默认空，生产需显式配置；变更治理在代码外。","source":"app/settings.py:8-12, 123-129; app/services/hydra_relay.py:609-623"},
        {"id":"hyd-04","phase":"Hydra 专用链","name":"研究标的与执行 universe 物理分层","priority":"P0","type":"hard","threat":"仅用于桥接/研究的标的从 model_hfq 泄漏到 execution_raw 或订单。","control":"raw symbols 必须恰好等于 live allowlist；research_only 只能在 model_hfq 且声明完整。","response":"BAD_REQUEST。","likelihood":"possible","now":"研究数据含额外标的是设计允许的，泄漏风险真实；代码已拦。","residual":"依赖 manifest 对 executable/research 标注正确并与 frame 一致。","source":"app/services/hydra_relay.py:654-719"},
        {"id":"hyd-05","phase":"Hydra 专用链","name":"四流 as_of、交易日与全标的覆盖","priority":"P0","type":"hard","threat":"混用不同截面的数据、非交易日决策或 execution_date 不是下一交易日。","control":"四 manifest as_of 一致；calendar 验证 decision 与 next trading day；model/raw 覆盖全部 target。","response":"BAD_REQUEST。","likelihood":"possible","now":"跨仓库交付最容易产生；代码已拦。","residual":"日历批次真实性仍取决于 publisher。","source":"app/services/hydra_relay.py:134-179"},
        {"id":"hyd-06","phase":"Hydra 专用链","name":"停牌与异常持仓阻断","priority":"P0","type":"hard","threat":"对停牌 ETF 下单，或专用 Hydra 账户混入白名单外持仓后错误计算 NAV/diff。","control":"原始执行价 as_of 检查 suspendFlag；账户 positions 必须合法且在 allowlist。","response":"BAD_REQUEST。","likelihood":"conditional","now":"ETF 停牌低频；账户混仓在配置/人工交易时可能。","residual":"只看 EOD suspendFlag，不是次日开盘实时可交易状态。","source":"app/services/hydra_relay.py:170-245"},
        {"id":"hyd-07","phase":"Hydra 专用链","name":"同内容幂等、同月异内容拒绝替换","priority":"P0","type":"hard","threat":"重试产生第二套订单，或同月研究结果被静默换篮子。","control":"basket_sha/domain/account 查已有；execution month 只允许一个 active target。","response":"返回原 attempt 或 HTTP 409。","likelihood":"possible","now":"网络重试和重复发布很现实；防线有效。","residual":"显式撤销/替换流程尚未定义。","source":"app/services/hydra_relay.py:182-220"},
        {"id":"hyd-08","phase":"Hydra 专用链","name":"调仓前初始化、对账与无未决单","priority":"P0","type":"hard","threat":"基于未知起始账本再平衡，或上一批未终局时叠加新订单。","control":"InstanceState 必须域/账户匹配且 reconciliation_status ok/reconciled；不得有 PENDING/PARTIAL。","response":"HTTP 409。","likelihood":"possible","now":"首次上线、部分成交或回报延迟时可能；已拦。","residual":"shared-ledger 的 attributed_ledger 状态与专用 Hydra 路径语义需保持区分。","source":"app/services/hydra_relay.py:737-784"},
        {"id":"hyd-09","phase":"Hydra 专用链","name":"live 限额配置必须完整且价格偏移≤50bps","priority":"P0","type":"hard","threat":"“开启 live”但额度为 0/缺失，或给出过宽限价。","control":"risk mode disabled/static/auto；static 所有限额正且 finite；auto 有订单 cap/buffer；offset 封顶。","response":"HTTP 423 或 BAD_REQUEST。","likelihood":"unknown","now":"仓库默认 disabled；生产批准与数值需会上核对。","residual":"auto 单笔上限≈NAV 很宽，是否符合小资金试运行需讨论。","source":"app/services/hydra_relay.py:53-109"},
        {"id":"hyd-10","phase":"Hydra 专用链","name":"逐批订单数/金额/持仓/现金二次风控","priority":"P0","type":"hard","threat":"订单过多、单笔或总换手过大、超卖、现金不足。","control":"_apply_risk_limits + 最坏买价/费率资金等式；client 还应查实时可用资金。","response":"BAD_REQUEST，不创建 attempt/orders。","likelihood":"possible","now":"每次 live 调仓都可能触发；server 侧有效。","residual":"server actual_cash/positions 来自请求/账本，不是下单瞬间 broker snapshot。","source":"app/services/hydra_relay.py:799-876, 525-541"},
        {"id":"hyd-11","phase":"Hydra 专用链","name":"retry 只能处理已对账 residual","priority":"P0","type":"hard","threat":"前一 attempt 未终局就重复补单，或拿未来/当日未冻结 raw 价格重试。","control":"无未决单、previous.status=RESIDUAL、raw as_of<trade_date、持仓/白名单复核。","response":"HTTP 409/BAD_REQUEST。","likelihood":"possible","now":"部分成交后正常会用；代码已拦越序。","residual":"重试统一 50bps，仍需 client 实时价格保护。","source":"app/services/hydra_relay.py:329-389"},
        {"id":"hyd-12","phase":"Hydra 专用链","name":"post-trade 精确对账后才结案","priority":"P0","type":"hard","threat":"订单状态看似结束但 QMT 现金/持仓与虚拟账本分叉，错误标 COMPLETE。","control":"close 前无未决单；positions 必须完全一致，cash 差≤1 元；否则拒绝。","response":"形成 COMPLETE 或 RESIDUAL，证据 hash 不可变。","likelihood":"possible","now":"费用、拒单、回报缺失时可能；闭环严格。","residual":"1 元现金容差是否适合所有费用/分红时点需确认。","source":"app/services/hydra_relay.py:391-476"},

        # Shadow boundary
        {"id":"shd-01","phase":"影子账本","name":"影子配置禁止 QMT 身份和订单能力","priority":"P0","type":"hard","threat":"研究对比实例意外跨入真实/模拟订单路径。","control":"qmt_account_id 必须空、orders_enabled 必须 false、mode 必须 shadow；service 不导入订单/成交模型。","response":"ShadowBoundaryError，实例不运行。","likelihood":"conditional","now":"新增影子实例时可能误配；代码已硬隔离。","residual":"未来重构必须保持 import/config 负向测试。","source":"app/services/shadow_ledger.py:100-136"},
        {"id":"shd-02","phase":"影子账本","name":"target 严格 schema、权重、日期与内容 hash","priority":"P0","type":"hard","threat":"错误/空/重复/未来 target 生成伪 NAV。","control":"列集合精确、代码唯一、finite 正权重和=1、as_of≤decision≤run、input hash。","response":"该实例 blocked，不生成快照。","likelihood":"possible","now":"外部 producer 文件交付时可能；代码已拦。","residual":"target_hash 基于规范 CSV，float 跨版本稳定性需保留测试。","source":"app/services/shadow_ledger.py:232-304"},
        {"id":"shd-03","phase":"影子账本","name":"sidecar provenance / 版本 / publisher allowlist","priority":"P0","type":"hard","threat":"target 数值正确但来源版本未经批准或旁路研究输入不可追溯。","control":"sidecar 字段与 target 一致，input_hash 重算，按实例限制 source_version/publisher commit。","response":"实例 blocked。","likelihood":"possible","now":"当前正式 Hydra shadow 已配置 pinned commit；其余 target 多数缺失。","residual":"未配置 allowlist 的 shadow 只验证格式，不验证批准版本。","source":"app/services/shadow_ledger.py:50-97, 179-227"},
        {"id":"shd-04","phase":"影子账本","name":"target 与估值价格时效硬限制","priority":"P0","type":"hard","threat":"陈旧 target 或陈旧 close 继续滚出看似正常的影子 NAV。","control":"max_target_age_days；每持仓/目标标的价格缺失、过旧或非正即失败。","response":"实例 blocked。","likelihood":"possible","now":"四个配置 target 文件当前缺失，现阶段会直接 blocked；Hydra shadow target 已存在。","residual":"时效按日历天；是否应按交易日需讨论。","source":"strategies.yaml; app/services/shadow_ledger.py:273-279, 406-426"},
        {"id":"shd-05","phase":"影子账本","name":"同 target hash 不重复调仓","priority":"P0","type":"hard","threat":"scheduler 每日运行对同一目标重复计成本/换手。","control":"state.target_hash 相同只估值，不 rebalance。","response":"transaction_cost/turnover 为 0，仅更新 NAV。","likelihood":"possible","now":"日调度+月/周目标下必然重复读取；代码已防。","residual":"同日 changed target 会累计成本，符合审计但需解释给使用者。","source":"app/services/shadow_ledger.py:370-398, 489-521"},
        {"id":"shd-06","phase":"影子账本","name":"影子买入不允许现金穿仓","priority":"P1","type":"hard","threat":"lot 舍入和最低佣金让理论组合现金变负。","control":"SELL first；BUY 数量按 lot 逐步减少直到 gross+fee≤cash。","response":"少买若干手，保留非负现金。","likelihood":"possible","now":"每次首次建仓/调仓均可能。","residual":"理论成交按收盘价，不模拟滑点、涨跌停或真实成交概率。","source":"app/services/shadow_ledger.py:435-483"},
        {"id":"shd-07","phase":"影子账本","name":"影子逐实例失败隔离","priority":"P1","type":"isolation","threat":"一个 target 缺失导致所有影子实例和主策略管线失败。","control":"run_all 对每实例 catch；写 blocked 状态；主/影子 scheduler 回调也分别 catch。","response":"其他实例继续，告警显示 blocked。","likelihood":"observed","now":"当前多个 target 缺失，正依赖该隔离。","residual":"scheduler 中主 pipeline 先跑，两个任务仍在同一进程/同一 job。","source":"app/services/shadow_ledger.py:306-331; app/scheduler/runtime.py"},

        # Ops / system
        {"id":"ops-01","phase":"启动与运行","name":"ownership 冲突时隔离 scheduler、保留诊断面","priority":"P0","type":"isolation","threat":"坏归属配置继续自动下单；或反过来让整个 HTTP 退出而失去冻结订单/诊断能力。","control":"startup validate_no_overlap；失败置 ownership_safe=false，不启动 scheduler，ready_degraded。","response":"自动编排隔离，HTTP 继续。","likelihood":"conditional","now":"当前 YAML 无显式重复 symbol；未来改配置可发生。","residual":"人工 /admin/run-pipeline 是否同样受 app.state.ownership_safe 限制需讨论（当前没有）。","source":"app/main.py:25-87; app/api/health.py"},
        {"id":"ops-02","phase":"启动与运行","name":"scheduler 默认关闭但仅按周一至周五","priority":"P0","type":"gap","threat":"节假日误跑、valid_date 选择错误，或以为已自动跑其实开关未开。","control":"scheduler_enabled 默认 false；cron timezone=Asia/Shanghai，day_of_week=mon-fri。","response":"无交易日历校验；job 把 today 直接传给 pipeline。","likelihood":"possible","now":"中国法定节假日必然出现；当前监控也明确 calendar_aware=false。","residual":"需统一“谁算下一交易日”和权威触发时刻。","source":"app/scheduler/runtime.py; app/services/ops_monitor.py:31-53"},
        {"id":"ops-03","phase":"启动与运行","name":"多 worker 会重复启动 scheduler","priority":"P0","type":"gap","threat":"每个 uvicorn worker 启一份 cron，并发清批次/写 SQLite/重生 order_id。","control":"仅代码注释建议 workers=1；无分布式锁或 leader election。","response":"依赖部署纪律与部分幂等。","likelihood":"conditional","now":"单 worker systemd 下低；扩容或误配 workers 时立即可能。","residual":"普通 Parquet 和 Trade 幂等也缺并发唯一性，影响会叠加。","source":"app/main.py:54-58"},
        {"id":"ops-04","phase":"监控与告警","name":"未采集遥测显式 missing","priority":"P0","type":"monitor","threat":"QMT 断线、tick/ACK 延迟未知，却在 dashboard 显示健康 0。","control":"live_snapshot coverage_gaps 明列 broker/tick/ack/intraday/sector/risk contribution missing。","response":"页面显示缺口，不伪造数值。","likelihood":"observed","now":"这些 P0/P1 遥测现在就是未接入，不是理论风险。","residual":"只有展示，没有前置阻断；live 前是否必须补齐需形成会议结论。","source":"app/services/ops_monitor.py; LIVE_DASHBOARD_MONITORING_SPEC_20260831.md"},
        {"id":"ops-05","phase":"监控与告警","name":"告警当前无持久确认/关闭闭环","priority":"P1","type":"gap","threat":"critical 只在有人刷新页面时计算，没人接收/确认，异常长期无人处理。","control":"AlertEngine 可检测 pipeline missing、stale order、orphan fill、divergence、shadow blocked。","response":"/admin/alerts 返回当下结果。","likelihood":"possible","now":"值班 sink 未接入；当前就是运行缺口。","residual":"需要持久 alert event、外部渠道、ack/close、runbook 与 SLA。","source":"app/services/alerts.py"},
    ],
    "questions": [
        {"id":"question-trigger","title":"权威触发与交易日","question":"行情入库、scheduler、人工触发、/hydra/live/trigger 各自负责什么？谁唯一决定 T+1 与节假日？","why":"当前 /market-data 不触发但响应称 triggered；scheduler 直接 run(today)，与部分文档的 T+1 流程不完全一致。"},
        {"id":"question-order-path","title":"统一订单风控还是保留双路径","question":"Hydra 专用 relay 直接写 PASS RawSignal/Order，是否应继续独立于通用 Precheck/Aggregate？","why":"专用限额更严，但两条订单创建路径会带来重复概念和 canary 血缘例外。"},
        {"id":"question-fail-open","title":"缺数据时统一语义","question":"探针缺失、volume 缺失、IOPV 缺失、pred 陈旧、策略异常分别应该 hard block、降级还是只告警？","why":"当前不同模块做法不一致，最危险的是缺数据时继续生成订单。"},
        {"id":"question-settlement","title":"结算完整性","question":"是否把 overfill、缺 mapping/signal、并发重复回报升级为数据库约束 + hard divergence？","why":"这些都是“真实成交已发生但虚拟账本可能错”的 P0 场景。"},
        {"id":"question-canary","title":"Canary 的账本归属","question":"server canary 成交应进入哪个实例/专用费用账本，还是必须强制平仓后仅做外部现金流？","why":"当前 canary Order 没有 order_signal_map，Settlement 会记成交但不更新任何 instance。"},
        {"id":"question-reconcile","title":"对账模型收口","question":"何时彻底退出 legacy owned_symbols=None，并把 shared-ledger total reconcile 变成下一批订单的权威前置条件？","why":"当前 shared、白名单和 legacy 三套语义同居，且 total mismatch 对普通策略只报警。"},
        {"id":"question-telemetry","title":"实盘前最小遥测","question":"QMT heartbeat、tick age、submit→ACK、first fill、盘中持仓里哪些是 live 开闸的必要条件？","why":"目前这些在 dashboard 被诚实标成 missing，但并不会阻止 live。"},
        {"id":"question-upload","title":"发布物原子性与校验时点","question":"普通行情与 strategy target 是否统一采用 temp+fsync+replace、内容 hash 和写入前 schema 校验？","why":"HydraDataStore 已具备成熟模式，普通 Parquet/upload 尚未复用。"},
        {"id":"question-readiness","title":"当前上线证据","question":"生产五个 live 开关、密钥隔离、publisher commit、QMT 账户指纹、四个缺失 shadow target 与五日观察证据分别由谁补？","why":"仓库代码通过不等于生产获准；最终审计仍是“不批准主策略模拟盘部署”。"},
    ],
}


def review_item_ids() -> set[str]:
    """All stable ids that may receive meeting comments/decisions."""
    ids = {item["id"] for item in REVIEW_CATALOG["flows"]}
    ids.update(item["id"] for item in REVIEW_CATALOG["boundaries"])
    ids.update(item["id"] for item in REVIEW_CATALOG["risks"])
    ids.update(item["id"] for item in REVIEW_CATALOG["questions"])
    return ids
