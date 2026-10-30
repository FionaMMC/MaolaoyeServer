# V713 股票腿估值与合成日终任务

本任务只读取已有实盘实例、月度状态、两腿净值及 OMS 证据，原子替换合成 JSON。SQLite 使用 `mode=ro` 和 `query_only=ON`，不建表、不估值、不创建实例、不划资金、不生成或领取交易订单。现有 Hydra 估值任务保持不变。

另有独立股票腿估值任务，复用 `materialize_live_performance`；SQLite 连接层只允许写 `perf_snapshots`、`perf_valuations`、`daily_risk_snapshots` 三张派生表。它不能创建或修改实例、资金、目标或订单，也不建表。两个新任务共同完成技术接线；真实行情证据仍须来自实际供应管道。

## 运行依赖与时间

- 新 release 包含 `scripts/run_v713_stock_valuation.py`、`scripts/run_v713_synthetic_eod.py`、合成服务修复及 `plugins/v713/synthetic_policy_v2.json`；沿用生产 Python 依赖。
- 工作目录仍为 `/opt/qmt-server/v2.3/server`，读取生产 `.env`，所以相对数据库路径仍指向生产。启动脚本使用 release **绝对路径**，`PYTHONPATH` 指向同一 release；不使用可能优先导入旧工作目录代码的 `python -m`。
- 股票腿估值北京时间工作日 **18:20 / 20:50** 运行，超时四分钟；合成报告为 **18:25 / 20:55**。现有 Hydra 估值为 18:15 / 20:45，超时五分钟。`After`/`Before` 只提供同时调度时的顺序，不主动启动或重配 Hydra 服务。即使某腿估值失败，报告仍严格检查两腿的期望交易日净值。
- 默认复用 `scripts.v713_cycle.exchange_dates`，从 Tushare 的上交所交易日历确定最近已收盘交易日。`TUSHARE_TOKEN` 由 systemd 的生产 `EnvironmentFile` 注入；不写入命令或回执。
- 可用 `QMT_V713_SYNTHETIC_CALENDAR=/absolute/path/calendar.json` 指定服务器私有日历证据。该文件必须含 `source`、`coverage_start`、`coverage_end`（YYYYMMDD）和 `trading_dates`（YYYYMMDD 字符串列表），覆盖运行当天。内容来自已验证的交易所日历，不能按工作日自行补齐。报告保存文件 SHA256。显式文件缺失或覆盖过期时等待，不静默改用另一来源。
- 16:00 上海时间以前不把当天视为已收盘；周末和休市日使用日历里的最近完成交易日。输出不含晚于期望交易日的净值。

## 安装命令（服务器 root，部署时执行）

将下列 release 变量替换为**已逐文件校验的新不可变 release**；本文件不假定 HTTP 当前代码路径等于生产主 checkout。

```sh
V713_RELEASE_SERVER=/opt/qmt-server/releases/REPLACE_WITH_REVIEWED_RELEASE/v2.3/server
test -f "$V713_RELEASE_SERVER/scripts/run_v713_synthetic_eod.py"
test -f "$V713_RELEASE_SERVER/scripts/run_v713_stock_valuation.py"
test -f "$V713_RELEASE_SERVER/plugins/v713/synthetic_policy_v2.json"
install -d -o qmtserver -g qmtserver -m 0750 /opt/qmt-server/v2.3/server/reports/v713-synthetic
sed "s|@V713_RELEASE_SERVER@|$V713_RELEASE_SERVER|g" "$V713_RELEASE_SERVER/deploy/qmt-v713-synthetic.service.in" > /tmp/qmt-v713-synthetic.service
sed "s|@V713_RELEASE_SERVER@|$V713_RELEASE_SERVER|g" "$V713_RELEASE_SERVER/deploy/qmt-v713-stock-valuation.service.in" > /tmp/qmt-v713-stock-valuation.service
systemd-analyze verify /tmp/qmt-v713-synthetic.service /tmp/qmt-v713-stock-valuation.service "$V713_RELEASE_SERVER/deploy/qmt-v713-synthetic.timer" "$V713_RELEASE_SERVER/deploy/qmt-v713-stock-valuation.timer"
install -o root -g root -m 0644 /tmp/qmt-v713-synthetic.service /etc/systemd/system/qmt-v713-synthetic.service
install -o root -g root -m 0644 "$V713_RELEASE_SERVER/deploy/qmt-v713-synthetic.timer" /etc/systemd/system/qmt-v713-synthetic.timer
install -o root -g root -m 0644 /tmp/qmt-v713-stock-valuation.service /etc/systemd/system/qmt-v713-stock-valuation.service
install -o root -g root -m 0644 "$V713_RELEASE_SERVER/deploy/qmt-v713-stock-valuation.timer" /etc/systemd/system/qmt-v713-stock-valuation.timer
systemctl daemon-reload
systemctl start qmt-v713-stock-valuation.service
systemctl start qmt-v713-synthetic.service
journalctl -u qmt-v713-stock-valuation.service -n 15 --no-pager
journalctl -u qmt-v713-synthetic.service -n 15 --no-pager
systemctl enable --now qmt-v713-stock-valuation.timer qmt-v713-synthetic.timer
systemctl list-timers qmt-v713-stock-valuation.timer qmt-v713-synthetic.timer --no-pager
```

默认结果为 `/opt/qmt-server/v2.3/server/reports/v713-synthetic/v713_synthetic.json`。目录为 qmtserver 的 0750，临时文件和替换后的 JSON 为 0600；不存入行情目录或 root-only 的 private 顶层。HTTP 服务也必须配置同一个 `QMT_V713_SYNTHETIC_OUTPUT` 才能通过 `/oms/live/v713/synthetic` 提供报告；在主服务当前部署流程中设置，不要复制另一份 JSON 冒充实时结果。

可选 `/opt/qmt-server/private/v713-synthetic.env` 由 systemd 读取，可以保持 root-only。它只用来放本任务的路径覆盖；不要让它覆盖版本固定的 `PYTHONPATH`。日历文件本身必须可由 qmtserver 读取。

## 验收与等待状态

`CURRENT` 必须同时满足：两实例为 live/attributed、有股票腿月度状态、两腿都有期望交易日净值、合成所需订单证据完整。核对 `policy_id=V713_SYNTHETIC_V2`、`primary_bps=13`、五条成本线 0/13/18/20/23、`expected_date` 与 `as_of_date` 相同、`database_read_only=true`。

缺日历、实例、月度状态、净值或订单证据时，分别输出 `WAITING_TRADING_CALENDAR`、`WAITING_INSTANCE`、`WAITING_START`、`WAITING_LEG_NAV`、`WAITING_ORDER_EVIDENCE`；输出空收益行，不补零收益或编造起始曲线。实例身份错误为 `WAITING_INSTANCE_CONFIGURATION`，输入收益等证据错误为 `WAITING_INPUT_EVIDENCE`。`REVIEW_REQUIRED` 保留人工复核状态。正常等待退出码也是 0，不能用 systemd 的 successful 代替业务验收。

数据库不可访问、SQL/schema 故障或输出目录不可写会使服务失败。查看日志并修复实际依赖；不要让任务建库或调整资金。停用使用 `systemctl disable --now qmt-v713-stock-valuation.timer qmt-v713-synthetic.timer`，不回滚数据库。

## 股票腿估值接线与仍需真实提供的原价证据

估值已由独立 `qmt-v713-stock-valuation.service` 接入。它固定估值 `live_v713_stockleg`，只估最新合格交易日，不自动回填；缺实例返回 `WAITING_INSTANCE`，尚未获资金的零资源实例返回 `WAITING_CAPITAL`，均不代建或注资。原价证据固定读取 `/opt/qmt-server/v2.3/server/reports/v713-synthetic/raw-price-evidence.json`。需要手动复验时使用同一个受写入范围限制的脚本：

```sh
cd /opt/qmt-server/v2.3/server
PYTHONPATH="$V713_RELEASE_SERVER" /opt/qmt-server/venv/bin/python "$V713_RELEASE_SERVER/scripts/run_v713_stock_valuation.py" --raw-price-evidence /opt/qmt-server/v2.3/server/reports/v713-synthetic/raw-price-evidence.json
```

这是单独的**派生估值写入**操作，不属于只读合成任务。证据 JSON 格式为实例 ID → 证券代码 → `{category, adjustment, source, sha256}`。每只当前持仓都须有 `adjustment=none`、可信原价来源、服务器 Parquet 分类及该文件的完整 SHA256；文件更新后由真实数据供应者更新哈希证据。脚本不会下载数据、制造证据、用账户快照价格代替日收盘价，或在证据文件缺失时改用旧策略元数据。原价行情文件保留在服务器，不下载到本地、不把复权行情换个标签冒充原价。证据文件应由 qmtserver 可读，建议 0600；不能为了让服务变绿而创建空的成功凭证。当前现金空仓不需要证券价格证明，但仍需要市场日历/指数数据和合法的 live attributed 实例。

缺价格证据时估值返回 `WAITING_RAW_PRICE_EVIDENCE`，缺收盘价时可能返回 `WAITING_PRICE_OR_LEDGER`；退出码 0 不表示当日已估值。新实例的首次划拨、月度目标发布、真实交易验收与日终原价供应仍分别按已授权流程完成，本只读任务不替代它们。
