# Dashboard 运维手册

## 目的

Dashboard 用于查看策略实例的虚拟账本、绩效、风险、策略状态和运行健康度。页面地址为 `/dashboard`，接口位于 `/admin/*`，均使用现有 API Key 鉴权；本手册不涉及密钥配置或认证呈现。

## 两层视图

### 当前实例视图

页面顶部下拉框选择一个实例后，以下内容必须全部按该实例刷新：

- KPI：累计收益、年化、Sharpe、最大回撤。
- NAV 曲线、收益曲线及周期收益。
- 策略状态、持仓数量和实例净值。
- 风险与运维页面中支持 `instance_id` 的诊断项。

浏览器会将最后一次选择保存到 `localStorage` 的 `qmt_dashboard_instance`。若无实例或切换异常，先刷新页面并确认 `/admin/health` 有该实例。

### 全部策略账本

概览页的“全部策略账本”列出所有实例的虚拟现金、持仓数、最新 NAV、日收益和快照日。它用于比较策略和影子账本，不是账户资产汇总。

特别注意：

- `reported_virtual_nav_sum` 是各虚拟账本的展示性求和。
- `reported_virtual_nav_sum_is_account_nav` 永远是 `false`。
- 多个实例可能共享同一个 QMT 账户，或者使用相同的虚拟初始资金；将它们相加会重复计算资金。
- `owned_symbols: []` 的实例会标记为 `shadow`，表示它不主张真实持仓归属。

真实账户资金应以券商/QMT 的账户权益和对账结果为准，不应使用 Dashboard 的实例 NAV 相加结果。

## V53 ETF 收益归因

当顶部选择 V53 实例（当前为 `paper_v53_v53`）并进入“策略内部”页时，会展示 ETF 持仓与单日收益归因表。

表中覆盖 V53 `owned_symbols` 定义的全部 ETF，即使当前仓位为零也会保留在表中。字段含义：

| 字段 | 定义 |
| --- | --- |
| 持仓 | 当前虚拟账本中的 ETF 股数 |
| 最新价 | `data/market/daily/etfs/{symbol}.parquet` 的最新收盘价 |
| ETF 日涨跌 | 最新收盘价相对前一条有效收盘价的变化 |
| 市值 | 持仓数量乘以最新价 |
| 权重 | ETF 市值除以该实例最新 NAV |
| 策略日贡献 | 当前权重乘以 ETF 日涨跌 |

“策略日贡献”是当前持仓在最新一个交易日的近似收益贡献，用来定位当日的主要驱动项。它不等同于持有期盈亏，也不包含现金、手续费、调仓成交价差和当日仓位变化的完整路径归因。

目前订单和成交表没有逐条 `instance_id` 成本血缘，因而 Dashboard 不展示虚假的单 ETF 成本价或持有期收益。若要增加这两项，需要先在订单、成交和账本层补充实例归属与成本基础。

## 只读接口

所有接口需要 `Authorization: Bearer <API_KEY>`：

| 接口 | 用途 |
| --- | --- |
| `GET /admin/health` | 实例清单、最新 NAV、数据和订单健康信息 |
| `GET /admin/metrics/summary?instance_id=...&period=30d` | 选定实例的 KPI |
| `GET /admin/nav-history?instance_id=...&period=30d` | 选定实例的 NAV 历史 |
| `GET /admin/strategy-state?instance_id=...` | 选定实例策略状态 |
| `GET /admin/portfolio-overview` | 全部虚拟账本总览；不代表真实账户 NAV |
| `GET /admin/etf-performance?instance_id=paper_v53_v53` | V53 ETF 行情、仓位和单日收益归因 |

示例：

```bash
curl -H "Authorization: Bearer $QMT_API_KEY" \
  http://127.0.0.1:8000/admin/etf-performance?instance_id=paper_v53_v53
```

## 日常检查顺序

1. 打开概览，确认当前实例、最新快照日和数据新鲜度。
2. 查看“全部策略账本”，确认实例是否都在更新；不要使用其 NAV 合计做资金判断。
3. 对 V53 查看 ETF 表，识别当日主要正负贡献，同时核对权重与预期配置。
4. 在运营与对账页检查告警、数据完整性和隔夜仓位异常。
5. 若发现实际 QMT 仓位与虚拟账本不一致，先运行对账流程；不要仅凭 Dashboard 修改账本。

## 部署、验证与回滚

服务由 `qmt-server.service` 托管。部署后至少执行：

```bash
cd /opt/qmt-server/v2.3/server
/opt/qmt-server/venv/bin/python -m py_compile app/api/dashboard.py app/api/admin_query.py
systemctl restart qmt-server.service
systemctl is-active qmt-server.service
```

随后以 API Key 调用 `/admin/portfolio-overview` 和 V53 的 `/admin/etf-performance`，确认返回成功。不要在日志、终端记录或文档中输出 API Key。

本次 Dashboard 更新在服务器上的备份目录为：

- `/opt/qmt-server/backups/dashboard_20260718/`
- `/opt/qmt-server/backups/dashboard_20260718_portfolio/`

仅在确认新版本导致问题时，才从对应备份复制 `app/api/dashboard.py` 与 `app/api/admin_query.py` 回服务目录，完成语法检查后重启服务。
