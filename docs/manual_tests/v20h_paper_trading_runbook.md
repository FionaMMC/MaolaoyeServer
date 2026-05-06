# V20H 模拟盘联调 Runbook（Windows）

把云端服务器上 V20H 已生成的 673 个买单**真实**下到 QMT 模拟盘账户，再把成交结果推回服务器闭环。

---

## 0. 前置条件清单

| 项 | 检查方法 |
|---|---|
| QMT 客户端已启动并**登录到模拟盘账号** | 看右下角账户号；**不要用真金账号** |
| QMT "交易"模块的 xttrader 接口已开启 | QMT 客户端 → 设置 → 交易 → 启用 xttrader |
| Windows 上有项目 git 仓库 | 已 clone `git@github.com:FionaMMC/MaolaoyeServer.git`；如果没有，跑 `git clone <url> C:\parttime\MaolaoyeServer` |
| Python venv 装好 xtquant + requests + pyyaml | `pip list` 看一下；缺的话 `pip install xtquant requests pyyaml` |
| 服务器有 V20H 信号 | 我已经触发过；`/orders?date=20260410` 返回 673 条 |

---

## 1. 一次性配置（首次跑前做一次）

### 1.1 拉最新代码

```powershell
cd C:\parttime\MaolaoyeServer
git pull origin master
```

期望看到 HEAD 是 `23f03d0` 或更新（含 `fix(server): strategies.yaml default path` 提交）。

### 1.2 设置环境变量（指向云服务器）

PowerShell 当前会话：
```powershell
$env:QMT_PIPELINE_BASE_URL = "http://120.26.138.82:8000"
$env:QMT_PIPELINE_API_KEY  = "pipeline-v23-shared-secret-2026"
$env:QMT_PIPELINE_PUSH_MODE = "server"
```

> 想做永久配置：系统 → 高级系统设置 → 环境变量 → 用户变量 → 新建上面三条。

### 1.3 在 strategies.yaml 里填模拟盘账户号

打开 `C:\parttime\MaolaoyeServer\v2.3\server\strategies.yaml`，把 `paper_v20h` 那个组的 `qmt_account_id` 从空字符串改成你的**模拟盘**证券账号（在 QMT 客户端"账号信息"看，长得像 `1234567890`）：

```yaml
- group_id: paper_v20h
  qmt_account_id: "你的模拟盘账号"   # ← 这里
  strategies:
    - strategy_id: v20h_v1_3
      virtual_initial_cash: 10000000
```

> ⚠️ **不要 commit 这个改动**——账户号是私有的。改完 `git status` 应该看到这个文件是 modified；不要 `git add` 它。

### 1.4 设置目标交易日

V20H 信号目前是为 **20260410** 这个日期生成的（我们用历史数据测的）。需要让 client 用这个日期而不是"明天"。

打开 `C:\parttime\MaolaoyeServer\v2.3\config.py`，找到这行：
```python
FORCE_TRADE_DATE: str | None = None
```
临时改成：
```python
FORCE_TRADE_DATE: str | None = "20260410"
```

> 同样**不要 commit** 这个改动；联调结束后改回 `None`。

---

## 2. 首次联调流程（按顺序跑）

### 步骤 1: 拉信号到本地 SQLite

```powershell
cd C:\parttime\MaolaoyeServer
python v2.3\client\order_query.py
```

期望日志：
```
PUSH_MODE=server，启动检查通过
FORCE_TRADE_DATE=20260410，强制作为目标交易日
服务器返回 673 条订单
写入 server_orders：新增 673 条
```

如果返回 0 条或者超时，去第 4 节"故障排查"。

### 步骤 2: 验证 SQLite 拿到了订单

```powershell
python -c "import sqlite3; c=sqlite3.connect('v2.3/pipeline.db'); print(c.execute('SELECT COUNT(*), valid_date FROM server_orders GROUP BY valid_date').fetchall())"
```

期望：
```
[(673, '20260410')]
```

抽查 5 条样本：
```powershell
python -c "import sqlite3; c=sqlite3.connect('v2.3/pipeline.db'); [print(r) for r in c.execute('SELECT order_id, symbol, direction, quantity, limit_price FROM server_orders LIMIT 5').fetchall()]"
```

### 步骤 3: ⚠️ 安全检查（下单前一定要做）

| 检查项 | 怎么验 |
|---|---|
| QMT 当前登录的是**模拟盘**账号 | 客户端右下角；账号末尾通常带 `(模拟)` |
| 模拟盘账户里有钱（建议 ≥ 1000 万） | 客户端 → 账号信息 → 可用资金 |
| `strategies.yaml` 的 `qmt_account_id` 跟 QMT 客户端显示一致 | 字符严格相等 |
| 没有别的脚本同时在用同一账户 | 看 QMT 委托窗口是否干净 |

任何一项不对就停，不要继续。

### 步骤 4: 下单到 QMT 模拟盘

```powershell
python v2.3\client\order_submit.py
```

> ⚠️ 这步会**真的**调 `xt_trader.order_stock()`。模拟盘没有真钱风险，但 QMT 会真的接收 673 个委托。

`order_submit.py` 的行为：
1. 读取 `server_orders` 表里 `valid_date=20260410` 的订单
2. 按 `account_group=paper_v20h` 分组，查模拟盘 `qmt_account_id`
3. 等到当日 09:15（联调跑当天 9:15 之前才会等；过了就立即开始）
4. 风控：检查可用资金 ≥ Σ qty × limit_price
5. 并发提交 8 路 `order_stock(... FIX_PRICE ...)`，每条记到 `local_orders`

期望结尾日志：
```
QMT 连接成功
处理 account_group=paper_v20h，qmt_account_id=...，共 673 笔
账户 ... 风控结果：通过 N 笔，拦截 (673-N) 笔
[多行 BUY 成功记录]
下单汇总：成功 N 笔，失败 (673-N) 笔
```

如果是 9:15 之前跑就会卡在 `等待至 09:15` 直到时间到。**演练时直接看脚本就好；正式就在 9:15 前几分钟跑**。

### 步骤 5: QMT 客户端肉眼验证

切到 QMT 客户端 → "委托" 窗口：
- 应该看到 N 条状态 = "已报"/"已成"/"部分成交" 的委托
- 检查随便几条：`股票代码` / `方向=买入` / `委托数量` / `委托价格` 跟服务器响应里的字段对得上
- 因为价格是 2026-04-10 的参考价，今天市场价大概率不一样，所以**多数委托会挂着不成交**——这正常，我们要的就是"提交动作"成功

### 步骤 6: 把成交结果推回服务器

`trade_result.py` 会查 QMT 当日成交、写到 `local_fills` 表，再 POST `/trade-result` 到云端。

```powershell
python v2.3\client\trade_result.py
```

期望日志最后：
```
查询 QMT 成交：N 条
POST /trade-result 200 ok
```

### 步骤 7: 服务器端确认收到

让我（我能从 Mac curl）：
```bash
curl -H "Authorization: Bearer pipeline-v23-shared-secret-2026" \
  "http://120.26.138.82:8000/orders?date=20260410" | python3 -m json.tool | head -50
```
我能看到 fill_status 已经更新就闭环成功。

---

## 3. 联调后清理

### 3.1 撤掉所有未成交挂单（可选，不撤也无所谓）

QMT 客户端 → 委托窗口 → 全选 → 撤单。

### 3.2 把 FORCE_TRADE_DATE 改回 None

```python
# v2.3/config.py
FORCE_TRADE_DATE: str | None = None
```

### 3.3 别把 strategies.yaml + config.py 的本地改动 commit 上去

```powershell
git status  # 应该看到 modified 但不要 git add 这俩
```

---

## 4. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `order_query.py` 返回 0 条订单 | FORCE_TRADE_DATE 没生效；或者服务器没数据 | 检查 `python -c "import config; print(config.FORCE_TRADE_DATE)"` 是否为 `'20260410'` |
| `GET /orders` 网络异常 | `QMT_PIPELINE_BASE_URL` 没设 / 设错 | `echo $env:QMT_PIPELINE_BASE_URL`，应是 `http://120.26.138.82:8000` |
| `account_group=paper_v20h 未找到 qmt_account_id` | strategies.yaml 没填账户号 | 回到 1.3 步 |
| `QMT 连接失败（返回值=...)` | QMT 客户端没开 / xttrader 接口没启用 / SESSION_ID 冲突 | 重启 QMT，确认右下角"交易"灯绿；改 `config.QMT_SESSION_ID` 试不同值 |
| 风控 100% 拦截 "资金不足" | 模拟盘可用资金 < 9 百万；或账号 ID 写错指到了真实小账户 | 给模拟盘充"钱"或者换账户 |
| QMT 看到一半委托但 `order_submit` 报错卡住 | 网络抖；线程池超时；QMT 内部限流 | 重新跑——`local_orders` 有 UNIQUE 索引会跳过已成功的，只补失败那部分 |
| `trade_result.py` 报 401/403 | API_KEY 没设 | 设 `$env:QMT_PIPELINE_API_KEY` |

---

## 5. 之后每天怎么跑（联调成功之后）

每个交易日的工作流（用户 = 我，搭档 = 你）：

```
T-1 收盘 后（晚上）
─ [我]   Mac 上跑 V20H ML pipeline → 上传 5 个 .parquet 到云端
─ [你]   Windows: python v2.3\client\trigger_pipeline.py   ← 让服务器算 T 信号

T 早上 09:00 之前
─ [你]   Windows: python order_query.py        ← 拉信号到 SQLite
─ [你]   Windows: 09:14 跑 python order_submit.py    ← 等到 09:15 自动提交
─ [你]   QMT 9:25 集合竞价 → 9:30 开盘成交

T 收盘 后
─ [你]   Windows: python trade_result.py    ← 推成交回服务器
```

`trigger_pipeline.py` 会自动算"下一交易日"作为 trade_date 调
`POST /admin/run-pipeline`，无需手 curl。结尾输出 `signals=N orders=N`
就证明触发成功；如果 `orders=0` 脚本会提示去查 `/admin/data-status`
排查 pred 是否覆盖目标日期。

可以全用 Windows Task Scheduler 自动化：
- T-1 晚上 22:00 → `python v2.3\client\trigger_pipeline.py`（等我刷完 pred 之后）
- T 早上 08:30 → `python order_query.py`
- T 09:14 → `python order_submit.py`
- T 15:30 → `python trade_result.py`

---

## 6. 把 FORCE_TRADE_DATE 改回 None 之后呢？

不再需要 FORCE_TRADE_DATE 是因为 V20H 数据是滚动更新的：
- 每天我会在 Mac 上重训 + 重传 V20H pred（或者 cron 自动），让 pred 包含 T-1
- 服务器运行 pipeline 时用 `trade_date=T`（next_trading_day）
- Windows 端 `order_query` 默认就是 `_get_next_trading_day(today)`，自动对齐

所以日常 ops 不需要碰 `FORCE_TRADE_DATE`——它只是首次联调的桥。
