# V7.13 部署与回滚手册

当前状态：禁止生产下单。以下步骤仅在研究审计解除阻塞后执行。

## 部署前置

1. 记录四仓库完整 commit 和所有输入 SHA-256。
2. 生成真实 Hydra、主篮子及三个 shadow target；逐一复核日期、权重、来源和 hash。
3. 为 `paper_v713` 提供未被其他 account-group 使用的专用 QMT 模拟账户。
4. 先保持 `plugins/v713/config.yaml: dry_run: true` 和 `orders_enabled:false` 上传篮子，执行两次 replay；第一次记录 target quantities，第二次必须命中 replay hash 跳过。
5. 确认数据库无该实例 `PENDING/PARTIAL`、`bookkeeping_divergence`，上次对账状态不是 pending/failed。

## 启用模拟盘

在 `strategies.yaml` 只改 V7.13 实例：填专用 `qmt_account_id`、设 `orders_enabled:true`、`account_isolation:dedicated`。确认该账户号没有出现在其他 group 后，再将 `plugins/v713/config.yaml` 的 `dry_run` 设为 `false`。重启单 worker 服务并检查启动日志。

首日必须保存：篮子 hash、SELL/BUY 顺序、订单、累计成交回报、费用、现金、持仓、NAV、target quantities、QMT 对账结果和 dashboard 截图。任何 `strict_rebalance_blocked` 或 `bookkeeping_divergence` 都停止下一调仓。

## 影子实例

将通过来源审计的文件放到 `plugins/v713/shadow_targets/<shadow_id>_latest.parquet`。不得把 shadow 放入 `account_groups`。检查 `/admin/shadow/summary` 和 `/admin/shadow/nav-history`，并验证同日 `orders/raw_signals/trades` 没有 shadow 血缘。

## 回滚

1. 立即把 V7.13 `orders_enabled` 设回 `false`，并将 relay `dry_run` 设回 `true`；重启服务。
2. 不删除订单、成交、instance state、shadow ledger 或 target；归档配置、DB、target 和日志。
3. 如存在未决订单，先由 QMT 侧确认/撤单并回传最终累计状态，再做对账。
4. 只有在 V7.13 和影子连续五个交易日健康、覆盖一次调仓且有恢复说明后，才按任务单归档并停用 v79；本轮不得清理。
