# QMT 期货接入需求文档（v2.4 client 工作量）

**版本**：v0.1（draft）
**日期**：2026-05-05
**目标版本**：v2.4
**面向角色**：Windows + QMT 客户端开发者
**前置依赖**：v2.3 已上线、纯股 pipeline 跑通

---

## 1. 为什么要做这件事

V20H（以及后续多个策略）依赖 **IC（中证1000股指期货）graduated_4 对冲**：

| V12 因子值 | 期货空头比例 |
|---|---|
| < Q10 | 100% 对冲 |
| Q10 ~ Q20 | 70% |
| Q20 ~ Q40 | 30% |
| > Q40 | 0%（不对冲） |

无期货支持时 V20H 只能跑"纯多头"版本，**真实超额收益预计从 +12.5% 掉到约 +5%，最大回撤从 -17% 放大到 -25%**。所以期货接入是 V20H 落地的硬前提。

---

## 2. 范围与不在范围

**v2.4 范围**：
- ✅ 沪深 300 / 中证 500 / 中证 1000 三大股指期货（IF / IC / IM 系列）
- ✅ 限价开仓 / 限价平仓 / 撤单
- ✅ 季度合约展期（roll day）
- ✅ 持仓 + 资金 + 保证金查询
- ✅ 与现有 stock pipeline 解耦的独立账户体系

**不在 v2.4 范围（明确排除）**：
- ❌ 商品期货（铜、原油等）
- ❌ 国债期货（虽然 5/10 年期可能有用，先不做）
- ❌ 期权
- ❌ 外盘期货
- ❌ 算法单（TWAP / VWAP）—— 全用限价单
- ❌ 跨期套利策略（先单合约就够 V20H 用）

---

## 3. Server 侧契约（v2.4 server 我自己会改）

让客户端开发者知道接口长什么样。**这部分搭档不用动**，给上下文。

### 3.1 新增端点

```
POST /futures-data            # 推期货行情（合约日线）
POST /futures-trade-result    # 推期货成交回报
GET  /futures-orders?date=    # 拉期货订单（与 /orders 并列）
```

### 3.2 strategies.yaml 扩展

```yaml
account_groups:
  - group_id: real_A_stock
    type: stock                          # 现有，纯股
    qmt_account_id: "1234567890"
    strategies: [...]

  - group_id: real_A_futures             # ⭐ 新增
    type: futures                        # ⭐ 新字段
    qmt_account_id: "FT_8888"            # ⭐ 期货资金账号（不同于股票）
    contracts:                           # ⭐ 允许交易的合约族
      - IC                               #   中证1000 期货
    strategies:
      - strategy_id: v20h_v1_3_hedge     # 跟 v20h_v1_3 配对的对冲器
```

策略 yaml 里 `account_group` 现在可以是 stock 或 futures 类型；同一 V20H 实例通常需要两个 account_group 联动（一个跑选股，一个跑对冲）。

### 3.3 RawSignal 扩展

新加 `FuturesSignal` 类型：
```
contract_code: str    # "IC2406.CFFEX"
side: str             # OPEN_LONG / OPEN_SHORT / CLOSE_LONG / CLOSE_SHORT
quantity: int         # 手数（不是股数）
limit_price: float
```

---

## 4. 客户端（QMT + Windows）必须交付的内容

### 4.1 数据采集（喂给 server）

| 字段 | 来源 | 说明 |
|---|---|---|
| 合约代码 | 自建合约清单 | 如 `IC2406.CFFEX`，需要识别主力 / 次主力 |
| 当日 OHLCV | xtquant `get_market_data` | period="1d"，跟股票同 |
| **持仓量 (open_interest)** | xtquant `get_market_data` | 期货独有字段 |
| **结算价 (settle)** | xtquant 或回测数据接口 | 用于盯市；如果 xtquant 不直供，用收盘价兜底 |
| 主力 / 次主力切换标志 | 自定义逻辑 | 看持仓量切换时间点；或者用第三方主力合约编号（如 IC9999.CFFEX） |

**交付物 1**：在 `v2.3/client/data_collector.py` 里加一个 `FuturesOHLCVCollector`：
- 输入：`config.FUTURES_CONTRACTS = ["IC", "IH", "IF", "IM"]`
- 输出：每个合约族 + 每个具体合约一个 parquet 文件
- 频率：每日 15:35（跟 stock 一起）

**交付物 2**：合约元数据采集：
- 合约乘数（IC = 200 元/点，IF = 300 元/点）
- 保证金率（动态从 broker 拿，或固定 15%）
- 到期日
- 上市日 / 退市日

存 `data/fundamentals/futures_contracts/{contract_code}.parquet`。

---

### 4.2 账户与持仓查询接口

需要在客户端实现以下函数（与现有 `qmt_state_mock.json` 同款，但加期货）：

```python
def query_futures_account(qmt_account_id: str) -> dict:
    """返回:
    {
        "available_cash": float,      # 期货账户可用资金
        "frozen_margin": float,        # 冻结保证金
        "available_margin": float,     # 可用保证金
        "today_pnl": float,            # 当日浮盈亏
        "total_equity": float,         # 总权益
    }
    """

def query_futures_positions(qmt_account_id: str) -> list[dict]:
    """返回每个合约的持仓:
    [
        {
            "contract_code": "IC2406.CFFEX",
            "side": "SHORT",            # LONG / SHORT
            "volume": 5,                # 手数
            "open_avg_price": 5800.0,
            "today_pnl": -1200.0,
            "occupied_margin": 87000.0,
        },
        ...
    ]
    """
```

**交付物 3**：在 `v2.3/client/order_submit.py` 里实现这两个函数（用 xttrader 的期货 API）。**第一次实现时必须验证 xttrader 能否在同一会话里订阅 stock + futures 两类账户**——若不行需建立第二个 XtQuantTrader 会话（这是开发注意事项 #8 已经标记的待验证项）。

---

### 4.3 下单接口

```python
def submit_futures_order(
    qmt_account_id: str,
    contract_code: str,         # "IC2406.CFFEX"
    side: str,                  # OPEN_LONG / OPEN_SHORT / CLOSE_LONG / CLOSE_SHORT
    volume: int,                # 手数
    price: float,               # 限价
    order_type: str = "FIX_PRICE",
) -> dict:
    """返回:
    {
        "ok": bool,
        "order_id": str | None,
        "submitted_at": str (ISO 8601),
        "fail_reason": str | None,    # "保证金不足" / "已超出涨跌停" 等
    }
    """
```

实现要点：
- 用 xttrader 的 `order_stock` 等价的期货接口（具体函数名待第一次开发时核实）
- 区分 4 个 side（不像股票只有 BUY/SELL）
- 返回的 order_id 必须能在 `query_futures_trades` 里查到

**交付物 4**：在 `v2.3/client/order_submit.py` 里加 `submit_futures_order` + 把 stock 和 futures 的下单结果都写进 SQLite `local_orders`（加 `instrument_type` 列区分）。

---

### 4.4 成交查询

```python
def query_futures_trades(qmt_account_id: str, trade_date: str) -> list[dict]:
    """当日所有期货成交:
    [
        {
            "contract_code": "IC2406.CFFEX",
            "order_id": "...",            # 对应 submit 时返回的 order_id
            "trade_id": "...",            # 单笔成交 ID
            "side": "OPEN_SHORT",
            "filled_volume": 1,
            "filled_price": 5798.0,
            "filled_time": "2026-05-05T09:30:12+08:00",
            "commission": 8.5,            # 期货手续费
        },
        ...
    ]
    """
```

**交付物 5**：在 `v2.3/client/trade_result.py` 里加期货成交查询，与 `query_stock_trades` 并行。

---

### 4.5 季度合约展期（roll day）

A 股股指期货每个季度展期一次：
- 主力合约通常是当月、季月
- 接近到期前 1-2 周，主力切到下季月
- **典型 roll day**：合约到期月份的 15 日 ~ 20 日

策略不直接处理 roll，由客户端在 roll day 自动：
1. 平掉旧主力的所有持仓
2. 在新主力上等量新开
3. 价差成本约 10-15 bps（设计文档里 V20H config 已记 `roll_cost_bps: 10`）

**交付物 6**：客户端实现一个判断函数：
```python
def is_roll_day(today: str) -> tuple[bool, str | None, str | None]:
    """返回 (是否 roll, 旧合约, 新合约)。"""
```
逻辑：根据当前主力合约的"距离到期天数"判断；超过阈值（如 7 天）就触发。

每个合约族（IC/IF/IM/IH）的 roll 日历独立。

---

### 4.6 配置文件改动

`v2.3/client/config.py` 增加：

```python
# ── 期货 ──
FUTURES_QMT_ACCOUNTS = {
    "real_A_futures": "FT_8888",     # account_group 到 QMT 期货资金账号映射
    "real_B_futures": "FT_9999",
}
FUTURES_CONTRACTS = ["IC", "IH", "IF", "IM"]   # 允许交易的期货族
FUTURES_ROLL_THRESHOLD_DAYS = 7                # 距到期 N 天触发 roll
FUTURES_MARGIN_BUFFER = 0.20                   # 保证金 20% 安全垫

# ── strategies.yaml 扩展支持 ──
# load_strategies() 现在要返回 type 字段（stock|futures）
```

---

## 5. 时间线建议

| 阶段 | 内容 | 工作量 | 依赖 |
|---|---|---|---|
| **W1** | 合约元数据 + 主力 / 次主力识别 + roll 日历 | 2-3 天 | 查阅 xtquant 期货 API 文档 |
| **W2** | FuturesOHLCVCollector + 期货行情入 Parquet | 1-2 天 | W1 |
| **W3** | query_futures_account / query_futures_positions | 1-2 天 | xttrader 期货登录验证 |
| **W4** | submit_futures_order + query_futures_trades | 2-3 天 | W3 |
| **W5** | roll day 自动展期 + 风控（保证金检查） | 2 天 | W4 |
| **W6** | 模拟盘联调 + e2e 测试 | 2-3 天 | All |

总工作量约 **2-3 周**全职等效，假定无 xtquant 期货 API 重大坑。

---

## 6. 关键风险点（开工前必须落实的事）

### 风险 #1：xtquant 期货 API 完整性未验证
- xttrader 是否支持期货下单？官方文档说支持，但平安证券模拟盘真实可用性需要测试
- **缓解**：开工前花 1 天写 5 行最小用例：登录期货账号 → 查持仓 → 下 1 手限价 → 撤单。能跑通才往下走

### 风险 #2：期货账户与股票账户分离
- 普通券商股票账户 ≠ 期货账户。期货是中金所，需要单独开户、单独登录
- xttrader 同一会话能订阅 stock + futures 两个账户吗？
- **缓解**：先确认搭档（或你）有期货模拟账号；如不能同会话则双 session

### 风险 #3：保证金动态变化
- 临近交割保证金会调高（10% → 15% → 20%）；遇国庆等大节假日也会调
- **缓解**：每日开盘前查一次实际保证金率，写日志；策略下单前留 20% 安全垫

### 风险 #4：合约切换日（最后交易日）数据不连续
- 主力切换瞬间，价格曲线会有跳变
- **缓解**：策略层面用"复权后主力指数"（IC9999）做信号；下单用真实合约。展期日的那天数据要单独标记

### 风险 #5：服务器侧 v2.4 schema 改动会破坏现有 strategies.yaml
- 现有 yaml 没 `type` 字段
- **缓解**：v2.4 server 升级时让 `type` 默认 `"stock"` 兼容旧配置

---

## 7. 验收标准

### 7.1 客户端单测
- `query_futures_account` mock 返回符合 schema
- `submit_futures_order` 4 个 side 都能 mock 出对应 xtconstant 调用
- `is_roll_day` 在合约月份 +-7 天附近返回正确

### 7.2 联调测试（模拟盘）
- 按 v20h_v1_3_hedge 策略生成的开仓信号能成功提交
- 平仓信号能撤掉持仓
- roll day 当天能完成换月（旧合约平 + 新合约开）
- 一周连续运行不出风控异常（保证金充足）

### 7.3 数据完整性
- 一周内所有交易日的 IC2406（或当时主力）日线行情能出现在 server 的 `data/market/daily/futures/`
- 持仓量字段不为 0
- 合约元数据有效（乘数 = 200）

---

## 8. 我（server 侧）的对应工作（不是给搭档看的）

**v2.4 server 端要做：**
1. 数据库 schema 加 `instrument_type` (`stock|futures`) 列到 orders / trades
2. 加 `futures_contracts` 表存合约元数据
3. Parquet 仓库新加 `data/market/daily/futures/{contract_code}.parquet`
4. ParquetStore 类加 `Category = "futures"`
5. 新加 `FuturesSignal` dataclass（与现有 `RawSignal` 并列）
6. AggregateService 知道按合约+side 分组（不能跟 stock 混）
7. SettlementService 处理期货独有字段（保证金占用变化）
8. 3 个新端点
9. V20H adapter 在 14d 启用 hedge 部分（输出 FuturesSignal[]）

**这部分等 14a-14c 跑通后单独写 v24 plan。**

---

## 9. 立即可以开工的事

不依赖 xtquant 期货 API 测试就能动手的：
1. ✅ 合约代码命名规范确认（IC2406.CFFEX vs CFFEX.IC2406 哪个 xtquant 用）
2. ✅ roll 日历表生成（基于已知到期日规则）
3. ✅ `is_roll_day` 函数 + 单测
4. ✅ FuturesOHLCVCollector 框架（先 mock，后接真实 xtquant）
5. ✅ 配置文件 schema（config.py + strategies.yaml）

**前 2 周可以做完前 4 项，期间 server 侧 v2.4 也开始准备 schema 改动。第 3 周 V20H 期货账户开通后就能合并 e2e。**

---

## 附录 A：相关文件清单

需要新建 / 修改的文件（提前知道改动面）：

```
v2.3/client/
├── config.py                     ← 加期货账户配置 + 合约族
├── strategies.yaml               ← 加 type 字段 + futures account_group
├── data_collector.py             ← 加 FuturesOHLCVCollector
├── order_query.py                ← 拉期货订单（GET /futures-orders）
├── order_submit.py               ← 加 submit_futures_order + query_futures_*
├── trade_result.py               ← 加 query_futures_trades + 推 /futures-trade-result
├── futures_calendar.py           ← 新建，roll day 判断
└── tests/                        ← 单测覆盖

v2.3/server/                      ← server 侧改动单写 v24 plan
plugins/v20h_adapter.py           ← 14d 启用 hedge 部分
```

---

## 附录 B：开工前核对单（搭档 / 你自己）

- [ ] 确认期货模拟账号能登录（券商已开通）
- [ ] 拿到期货账户的 fund_account 编号
- [ ] xtquant 期货 API 名字落实：`xtdata.get_market_data` 期货 contract_code 怎么写？xttrader 期货下单函数叫什么？
- [ ] 主力合约识别规则：用持仓量阈值还是用第三方主力合约编号（IC9999.CFFEX）？
- [ ] roll day 阈值：距到期几天？（建议 7 天，保留缓冲）
- [ ] 保证金安全垫：建议 20%（高于 broker 实际值留缓冲）
- [ ] 与策略方对齐：V20H 用 IC（中证 1000）；其他策略可能用 IF/IH
- [ ] 跟 server 侧对齐：什么时候开 v2.4 server 端开发（建议 client W3 完成后）

---

**版本历史:**
- v0.1 (2026-05-05): 初稿，基于 V20H 接入需求倒推
