# QMT Pipeline — 服务器端 API 接口文档（纯股）v2.1

**版本**：v2.1  
**日期**：2026-04-28  
**用途**：供服务器端开发者参考，定义本地客户端与服务器之间的全部接口  
**协议**：HTTP/HTTPS，数据格式 JSON，编码 UTF-8  
**范围**：仅股票

---

## 接口总览

| 编号 | 方法 | 路径 | 说明 | 调用时间 |
|------|------|------|------|---------|
| 1 | POST | `/market-data` | 本地推送当日行情（股票 + 指数 + ETF） | 每日 15:35 |
| 2 | POST | `/trade-result` | 本地推送收盘成交回报 | 每日 15:30 |
| 3 | GET | `/orders` | 本地查询次日归集订单 | 每日 19:00 |

---

## 认证方式

所有请求须在 Header 中携带 API Key：

```
Authorization: Bearer {API_KEY}
```

API Key 由服务器方生成并提供给本地客户端。

---

## 通用规范

### 时间格式

- 日期字段（如 `trade_date`、`valid_date`）：`YYYYMMDD`，例如 `20260428`
- 时间戳字段（如 `filled_time`）：ISO 8601，例如 `2026-04-28T15:32:00+08:00`

### 通用响应结构

所有接口均返回以下结构：

```json
{
  "code": 0,
  "message": "ok",
  "data": { ... }
}
```

- `code: 0` 表示成功，非 `0` 表示失败（见错误码定义）
- `message`：可读描述
- `data`：业务数据，失败时可为 null

---

## 接口一：推送当日行情

**方法**：POST  
**路径**：`/market-data`  
**调用时间**：每日 15:35  
**说明**：本地在每个交易日收盘后，将当日日线行情推送至服务器，触发策略运算。行情包含股票、指数、ETF 三类标的。

### 请求体

```json
{
  "trade_date": "20260428",
  "stocks": [
    {
      "symbol": "600519.SH",
      "open": 1520.00,
      "high": 1548.00,
      "low": 1515.00,
      "close": 1540.00,
      "volume": 12345678,
      "amount": 19012345678.00,
      "turnover_rate": 0.0032,
      "is_suspended": false
    }
  ],
  "indexes": [
    {
      "symbol": "000300.SH",
      "open": 3800.00,
      "high": 3850.00,
      "low": 3790.00,
      "close": 3820.00,
      "volume": 0,
      "amount": 0.00
    }
  ],
  "etfs": [
    {
      "symbol": "510300.SH",
      "open": 3.800,
      "high": 3.850,
      "low": 3.790,
      "close": 3.820,
      "volume": 100000000,
      "amount": 381000000.00,
      "turnover_rate": 0.0015,
      "is_suspended": false
    }
  ]
}
```

### 字段说明

**顶层字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `trade_date` | string | 是 | 交易日，格式 YYYYMMDD |
| `stocks` | array | 是 | 股票日线，全市场推送；当日停盘时传空数组 |
| `indexes` | array | 是 | 指数日线；至少包含策略依赖的主要指数 |
| `etfs` | array | 是 | ETF 日线；至少包含策略依赖的 ETF |

**stocks[] 和 etfs[] 字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `symbol` | string | 是 | 标的代码，如 `600519.SH` |
| `open` | float | 是 | 开盘价 |
| `high` | float | 是 | 最高价 |
| `low` | float | 是 | 最低价 |
| `close` | float | 是 | 收盘价 |
| `volume` | int | 是 | 成交量（股）；停牌时填 0 |
| `amount` | float | 是 | 成交额（元）；停牌时填 0 |
| `turnover_rate` | float | 否 | 换手率，0~1；停牌时填 0 |
| `is_suspended` | bool | 是 | 是否停牌 |

**indexes[] 字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `symbol` | string | 是 | 指数代码，如 `000300.SH` |
| `open` | float | 是 | 开盘点位 |
| `high` | float | 是 | 最高点位 |
| `low` | float | 是 | 最低点位 |
| `close` | float | 是 | 收盘点位 |
| `volume` | int | 否 | 成交量；QMT 不提供时填 0 |
| `amount` | float | 否 | 成交额（元）；QMT 不提供时填 0 |

### 响应体

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "trade_date": "20260428",
    "received": {
      "stocks": 4856,
      "indexes": 7,
      "etfs": 42
    },
    "strategy_triggered": true
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `received.stocks` | int | 成功入库的股票数量 |
| `received.indexes` | int | 成功入库的指数数量 |
| `received.etfs` | int | 成功入库的 ETF 数量 |
| `strategy_triggered` | bool | 策略是否已触发运算（异步，非最终结果） |

---

## 接口二：推送收盘成交回报

**方法**：POST  
**路径**：`/trade-result`  
**调用时间**：每日 15:30  
**说明**：本地在收盘后推送全天最终成交结果。服务器根据 `order_id` 查询映射表，将成交按比例拆分到原始信号，更新策略虚拟状态。

**与 v1.0 的差异**：
- 不再有 `stage` 字段，仅有收盘一次推送
- 匹配键由 `signal_id` 改为 `order_id`（服务器维护 order→signal 映射表）

### 请求体

```json
{
  "trade_date": "20260428",
  "results": [
    {
      "order_id": "550e8400-e29b-41d4-a716-446655440000",
      "filled_quantity": 300,
      "filled_price": 1547.70,
      "filled_time": "2026-04-28T14:55:32+08:00",
      "status": "FILLED"
    },
    {
      "order_id": "550e8400-e29b-41d4-a716-446655440001",
      "filled_quantity": 100,
      "filled_price": 14.85,
      "filled_time": "2026-04-28T09:25:18+08:00",
      "status": "PARTIAL"
    },
    {
      "order_id": "550e8400-e29b-41d4-a716-446655440002",
      "filled_quantity": 0,
      "filled_price": 0,
      "status": "CANCELLED"
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `trade_date` | string | 是 | 成交发生的交易日，格式 YYYYMMDD |
| `results` | array | 是 | 成交回报列表；当日无任何成交时传空数组 |
| `results[].order_id` | string | 是 | 服务器订单 ID（来自 GET /orders 返回的 order_id） |
| `results[].filled_quantity` | int | 是 | 实际成交总数量（股）；未成交填 0 |
| `results[].filled_price` | float | 是 | 实际成交均价（元）；未成交填 0 |
| `results[].filled_time` | string | 否 | 成交时间，ISO 8601；未成交可省略 |
| `results[].status` | string | 是 | 见枚举值：FILLED / PARTIAL / CANCELLED / REJECTED |

**注意**：`filled_quantity` 为当日该订单的全天汇总成交数量（多笔成交已在本地聚合），`filled_price` 为加权均价。

### 响应体

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "trade_date": "20260428",
    "matched_count": 2,
    "unmatched_order_ids": []
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `matched_count` | int | 成功匹配到订单的回报数量 |
| `unmatched_order_ids` | array | 未能匹配到订单的 order_id 列表，用于排查异常 |

**本地处理**：若 `unmatched_order_ids` 非空，触发微信报警并人工核查。

---

## 接口三：查询次日归集订单

**方法**：GET  
**路径**：`/orders`  
**调用时间**：每日 19:00  
**说明**：本地查询次日的归集订单。订单由服务器对原始信号完成预检查和归集后生成，每条订单对应本地一笔真实 QMT 委托。

### 服务器返回语义

| code | orders 数量 | 含义 | 本地处理 |
|------|------------|------|---------|
| 0 | 非空 | 策略完成，有交易机会 | 写入本地 SQLite，微信通知 |
| 0 | 空（`[]`） | 策略完成，当日无交易机会 | 微信提示（非报警） |
| 3002 | — | 策略尚未完成运算 | 等 30 分钟重试一次；仍 3002 则微信报警 |

本地遇到以下情况触发报警：

- 网络超时或连接拒绝（服务器完全无响应）
- code 3002 且重试后仍 3002
- 其他非预期 code

### 请求参数

| 参数 | 位置 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| `date` | query | string | 是 | 查询目标交易日，格式 YYYYMMDD，填次日日期 |

示例：

```
GET /orders?date=20260429
Authorization: Bearer {API_KEY}
```

### 响应体（有订单）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "date": "20260429",
    "orders": [
      {
        "order_id": "550e8400-e29b-41d4-a716-446655440000",
        "account_group": "real_A",
        "symbol": "600519.SH",
        "direction": "BUY",
        "quantity": 300,
        "limit_price": 1560.00,
        "valid_date": "20260429"
      },
      {
        "order_id": "550e8400-e29b-41d4-a716-446655440001",
        "account_group": "real_A",
        "symbol": "000001.SZ",
        "direction": "SELL",
        "quantity": 200,
        "limit_price": 14.80,
        "valid_date": "20260429"
      },
      {
        "order_id": "550e8400-e29b-41d4-a716-446655440002",
        "account_group": "real_B",
        "symbol": "600036.SH",
        "direction": "BUY",
        "quantity": 100,
        "limit_price": 38.50,
        "valid_date": "20260429"
      }
    ]
  }
}
```

### 响应体（无订单）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "date": "20260429",
    "orders": []
  }
}
```

### 订单字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `order_id` | string | 订单唯一 ID（UUID），成交回报中用于匹配 |
| `account_group` | string | 账户组标识；本地根据 strategies.yaml 映射到对应 QMT 账户 |
| `symbol` | string | 标的代码，如 `600519.SH` |
| `direction` | string | BUY 或 SELL |
| `quantity` | int | 归集后总数量（股）；BUY 必为 100 的整数倍；SELL 清仓尾单可为非整百 |
| `limit_price` | float | 限价；BUY 为参与归集的最高出价，SELL 为最低卖价 |
| `valid_date` | string | 有效交易日，格式 YYYYMMDD；本地校验此字段与次日日期匹配后才执行 |

**注意**：同一响应中可能包含不同 `account_group` 的订单，本地分别查找对应 QMT 账户处理。

---

## 错误码定义

| code | 说明 | 本地处理建议 |
|------|------|------------|
| `0` | 成功 | 正常处理 |
| `1001` | 认证失败（API Key 无效或缺失） | 检查配置，报警 |
| `1002` | 请求参数不合法 | 检查本地数据，报警 |
| `2001` | 行情数据日期重复（已存在该交易日数据） | 记录日志，不报警（可能是重推） |
| `2002` | 行情数据为空（三个数组均为空） | 检查下载模块，报警 |
| `3001` | 查询日期非交易日 | 检查交易日历逻辑，报警 |
| `3002` | 策略尚未完成运算（可稍后重试） | 延迟 30 分钟重试一次，仍失败则报警 |
| `4001` | 成交回报中所有 order_id 均未匹配到订单 | 报警，人工核查（可能配置错误或 order_id 来源错误） |
| `5000` | 服务器内部错误 | 报警，人工核查 |

---

## 枚举值

### direction

| 值 | 说明 |
|----|------|
| `BUY` | 买入 |
| `SELL` | 卖出 |

### status（成交状态，用于 POST /trade-result）

| 值 | 说明 |
|----|------|
| `FILLED` | 全部成交 |
| `PARTIAL` | 部分成交 |
| `CANCELLED` | 已撤单（含未成交部分） |
| `REJECTED` | 被拒绝（如涨跌停、账户异常等） |

---

## 本地调用流程示意（伪代码）

```python
# 每日 15:30：推送收盘成交回报
raw_trades = qmt.query_stock_trades(date=today)
results = aggregate_by_order_id(raw_trades)   # 多笔成交聚合为均价
resp = POST /trade-result {
    "trade_date": today,
    "results": results   # [{order_id, filled_quantity, filled_price, status}, ...]
}
if resp.code not in (0,):
    wechat_alert("成交回报推送失败: " + resp.message)
elif resp.data.unmatched_order_ids:
    wechat_alert("存在未匹配订单: " + resp.data.unmatched_order_ids)

# 每日 15:35：推送行情
market = download_market_data(today)   # stocks + indexes + etfs
resp = POST /market-data {
    "trade_date": today,
    "stocks": market.stocks,
    "indexes": market.indexes,
    "etfs": market.etfs
}
if resp.code not in (0, 2001):
    wechat_alert("行情推送失败: " + resp.message)

# 每日 19:00：拉取次日订单
next_date = get_next_trade_date()
try:
    resp = GET /orders?date={next_date}
except NetworkError:
    wechat_alert("订单拉取失败：服务器无响应")
    exit

if resp.code == 3002:
    sleep(30min)
    try:
        resp = GET /orders?date={next_date}
    except NetworkError:
        wechat_alert("重试时服务器无响应")
        exit
    if resp.code == 3002:
        wechat_alert("策略运算超时，请人工检查服务器")
        exit

if resp.code != 0:
    wechat_alert("订单拉取失败: " + resp.message)
    exit

if not resp.data.orders:
    wechat_notify("今日无交易机会")
else:
    valid_orders = [o for o in resp.data.orders if o.valid_date == next_date]
    sqlite.insert_server_orders(valid_orders)
    wechat_notify(f"已制单 {len(valid_orders)} 条")

# 次日 09:10：竞价下单
orders = sqlite.query("SELECT * FROM server_orders WHERE valid_date=?", [today])
for order in orders:
    account_cfg = config.get_account(order.account_group)
    qmt_account_id = account_cfg.qmt_account_id

    if order.direction == "BUY":
        available_cash = qmt.query_stock_asset(qmt_account_id).available_cash
        if order.quantity * order.limit_price > available_cash:
            wechat_alert(f"资金不足，跳过 {order.symbol} BUY {order.quantity}股")
            sqlite.update_local_order(order.order_id, submit_status="FAILED",
                                      fail_reason="资金不足")
            continue
    else:  # SELL
        position = qmt.query_stock_positions(qmt_account_id, order.symbol).available_qty
        if order.quantity > position:
            wechat_alert(f"持仓不足，跳过 {order.symbol} SELL {order.quantity}股")
            sqlite.update_local_order(order.order_id, submit_status="FAILED",
                                      fail_reason="持仓不足")
            continue

    local_order_id = qmt.passorder(
        account_id=qmt_account_id,
        symbol=order.symbol,
        direction=order.direction,
        quantity=order.quantity,
        price=order.limit_price,
        order_type=FIX_PRICE
    )
    sqlite.insert_local_order(local_order_id, order, qmt_account_id)
```
