# v2.3 Server 端总体设计

**日期**：2026-05-04
**版本**：v2.3
**对应 client**：`v2.3/client/`（已就绪，e2e mock_qmt 跑通）
**API spec**：`API接口文档（纯股）v2.1.md`（v2.3 沿用，未升版）

---

## 1. 范围

实现满足以下契约的 HTTP 服务器：

| 端点 | 方法 | 用途 |
|---|---|---|
| `/market-data` | POST | 接收 client 推送的当日股票/指数/ETF 行情，入库 + 触发策略 |
| `/orders` | GET | 返回指定交易日的归集订单（client 19:00 拉取）|
| `/trade-result` | POST | 接收 client 推送的成交回报，更新虚拟账本 + 绩效 |
| `/healthz` | GET | 健康检查（k8s/lb 标准） |
| `/readyz` | GET | 就绪检查（依赖 DB+Parquet 路径就绪）|

加上后台模块：策略引擎（多实例 + 插件式）、预检引擎、归集引擎、绩效引擎、调度器。

---

## 2. 技术栈与决策

| 关注点 | 选择 | 备选未选 | 理由 |
|---|---|---|---|
| 语言/版本 | Python 3.11 | — | 与 client 对齐 |
| HTTP 框架 | FastAPI 0.110+ | Flask | 类型注解、async、auto-doc、依赖注入 |
| ASGI server | uvicorn[standard] | gunicorn+uvicorn worker | 部署简单 |
| Schema | Pydantic v2 | dataclasses | FastAPI 默认；序列化/反序列化一体 |
| ORM | SQLAlchemy 2.0 | tortoise / peewee | 行业标准，迁移生态完整 |
| DB | SQLite（默认）→ PostgreSQL（环境切换） | 永远 PG | 起步轻量，Pydantic Settings 一行切换 |
| 配置 | pydantic-settings | py 常量、yaml | 12-factor compliant |
| 时间序列存储 | pyarrow Parquet | DuckDB / TimescaleDB | 与 client `data_collector.py` 一致 |
| 调度 | APScheduler 3.x（in-process） | Celery+beat / cron | 单机部署够用 |
| 日志 | structlog | loguru / stdlib | 结构化 JSON，对接 ELK/Loki 友好 |
| 测试 | pytest + pytest-asyncio + httpx.AsyncClient | unittest | FastAPI 官方推荐 |
| Lint/format | ruff + black（可选） | flake8 | 一体化、快 |
| 部署 | systemd + uvicorn | docker compose / k8s | 阿里云 ECS 单机起步 |

**为什么不直接 Docker**：现阶段单台 ECS 足够，systemd 操作直观便于排障。规模上来后再 docker-ize。

---

## 3. 模块划分与目录布局

```
v2.3/server/
├── pyproject.toml                  # server 端独立依赖
├── .env.example                    # pydantic-settings 模板
├── app/
│   ├── __init__.py
│   ├── main.py                     # FastAPI app 入口 + lifespan
│   ├── settings.py                 # pydantic-settings (Settings class)
│   ├── logging_setup.py            # structlog 配置
│   ├── db.py                       # SQLAlchemy engine + session_factory
│   ├── auth.py                     # Bearer token middleware
│   ├── exceptions.py               # 业务错误码定义 (1001/2001/3001/...)
│   ├── api/                        # ← HTTP 路由层
│   │   ├── __init__.py
│   │   ├── market_data.py          # POST /market-data
│   │   ├── orders.py               # GET /orders
│   │   ├── trade_result.py         # POST /trade-result
│   │   └── health.py               # /healthz, /readyz
│   ├── models/                     # ← SQLAlchemy ORM
│   │   ├── __init__.py
│   │   ├── instance_state.py       # 策略实例虚拟账本
│   │   ├── raw_signals.py          # 原始信号
│   │   ├── orders.py               # 归集订单
│   │   ├── order_signal_map.py     # order ↔ signal 映射（拆单用）
│   │   ├── trades.py               # 成交回报
│   │   └── perf.py                 # 绩效快照
│   ├── schemas/                    # ← Pydantic 请求/响应
│   │   ├── __init__.py
│   │   ├── market_data.py          # POST /market-data
│   │   ├── orders.py               # GET /orders 响应
│   │   ├── trade_result.py         # POST /trade-result
│   │   └── common.py               # 通用响应包装 {code, message, data}
│   ├── services/                   # ← 业务逻辑（无 HTTP 依赖）
│   │   ├── __init__.py
│   │   ├── ingest.py               # 行情入库
│   │   ├── settlement.py           # 成交回报处理 + 拆单 + 虚拟账本更新
│   │   ├── precheck.py             # 预检（资金/持仓/手数）
│   │   ├── aggregate.py            # 归集引擎
│   │   ├── orders_queue.py         # 订单队列查询
│   │   └── perf.py                 # NAV 计算
│   ├── strategy/                   # ← 策略框架
│   │   ├── __init__.py
│   │   ├── base.py                 # Strategy 抽象基类
│   │   ├── context.py              # Context（注入给策略的运行时上下文）
│   │   ├── loader.py               # 插件加载器（扫描 plugins/）
│   │   └── runner.py               # 触发所有实例运算并收集 RawSignal
│   ├── storage/                    # ← 文件存储
│   │   ├── __init__.py
│   │   └── parquet.py              # 读写 Parquet（行情 + 增量追加 + 去重）
│   └── scheduler/                  # ← APScheduler 任务编排
│       ├── __init__.py
│       └── jobs.py                 # 定时任务定义
├── plugins/                        # ← 策略插件目录（用户丢 .py 进来即可）
│   ├── README.md
│   └── _example_buy_and_hold.py    # 参考实现 + 插件契约示例
├── data/                           # ← server 端 Parquet 仓库（与 client 独立）
│   └── market/daily/{stocks,etfs,indexes}/{symbol}.parquet
├── pipeline-server.db              # SQLite 业务数据
├── tests/
│   ├── conftest.py                 # 共享 fixtures（tmp DB, tmp Parquet, test client）
│   ├── unit/                       # 纯函数测试
│   ├── integration/                # 真实 DB + 真实 Parquet
│   └── e2e/                        # FastAPI TestClient 全链路
└── deploy/
    ├── systemd/qmt-server.service
    ├── nginx-example.conf          # 反代到 uvicorn（可选）
    └── README.md                   # 阿里云部署完整步骤
```

---

## 4. 与 client 的契约（API 字段精确对照）

### `POST /market-data`
**请求**：
```json
{
  "trade_date": "20260430",
  "stocks":  [{symbol, open, high, low, close, volume, amount, is_suspended}, ...],
  "indexes": [{symbol, open, high, low, close, volume, amount}, ...],   // 无 is_suspended
  "etfs":    [{symbol, open, high, low, close, volume, amount, is_suspended}, ...]
}
```
**响应**：
```json
{
  "code": 0,
  "data": {"trade_date":"...", "received":{"stocks":N,"indexes":N,"etfs":N}, "strategy_triggered":true}
}
```
**重复推送**：返回 `code=2001`（client 视为成功）。

### `GET /orders?date=YYYYMMDD`
**响应（有订单）**：
```json
{
  "code": 0,
  "data": {
    "date": "20260430",
    "orders": [{order_id, account_group, symbol, direction, quantity, limit_price, valid_date}, ...]
  }
}
```
**响应（无订单）**：`data.orders=[]`
**响应（策略未完成）**：`code=3002`（client 30 分钟后重试一次）

### `POST /trade-result`
**请求**：
```json
{
  "trade_date": "20260430",
  "results": [{order_id, filled_quantity, filled_price, status, filled_time?}, ...]
}
```
status ∈ {FILLED, PARTIAL, CANCELLED, REJECTED}
**响应**：
```json
{
  "code": 0,
  "data": {"trade_date":"...", "matched_count":N, "unmatched_order_ids":[]}
}
```

### 鉴权
所有端点 header 必带 `Authorization: Bearer {API_KEY}`。失败返回 `code=1001`。

---

## 5. SQLite Schema（initial）

```sql
CREATE TABLE instance_state (
    instance_id        TEXT PRIMARY KEY,        -- "{group_id}_{strategy_id}"
    virtual_cash       REAL NOT NULL,
    virtual_positions  TEXT NOT NULL,           -- JSON dict
    last_update        TEXT NOT NULL            -- ISO 8601
);

CREATE TABLE raw_signals (
    signal_id          TEXT PRIMARY KEY,
    instance_id        TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    direction          TEXT NOT NULL,
    quantity           INTEGER NOT NULL,
    reference_price    REAL NOT NULL,
    price_offset       REAL NOT NULL,
    limit_price        REAL NOT NULL,
    valid_date         TEXT NOT NULL,
    signal_time        TEXT NOT NULL,
    precheck_status    TEXT NOT NULL,           -- PASS / FAIL
    precheck_reason    TEXT
);

CREATE TABLE orders (
    order_id           TEXT PRIMARY KEY,
    account_group      TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    direction          TEXT NOT NULL,
    quantity           INTEGER NOT NULL,
    limit_price        REAL NOT NULL,
    valid_date         TEXT NOT NULL,
    status             TEXT NOT NULL,           -- PENDING / FILLED / PARTIAL / EXPIRED
    created_at         TEXT NOT NULL
);

CREATE TABLE order_signal_map (
    order_id           TEXT NOT NULL,
    signal_id          TEXT NOT NULL,
    signal_quantity    INTEGER NOT NULL,
    PRIMARY KEY (order_id, signal_id)
);

CREATE TABLE trades (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id           TEXT NOT NULL,
    filled_quantity    INTEGER NOT NULL,
    filled_price       REAL NOT NULL,
    filled_time        TEXT,
    status             TEXT NOT NULL,
    received_at        TEXT NOT NULL
);

CREATE TABLE perf_snapshots (
    instance_id        TEXT NOT NULL,
    date               TEXT NOT NULL,           -- YYYYMMDD
    nav                REAL NOT NULL,
    daily_return       REAL,
    positions_snapshot TEXT NOT NULL,           -- JSON dict
    PRIMARY KEY (instance_id, date)
);
```

---

## 6. Parquet 仓库布局（server 独立于 client）

```
v2.3/server/data/market/daily/
├── stocks/{symbol}.parquet      ← 接收 client POST /market-data 增量追加
├── etfs/{symbol}.parquet
└── indexes/{symbol}.parquet
```

**初次部署** bootstrap：从 client 的 `v2.3/data/` rsync 一次过来（约 65 MB）。后续每日增量自动累积。详见 plan 13。

**写入规则**：按 `trade_date` 主键去重 + append + 按时间升序。复用 `data_collector.py` 里 `append_to_parquet` 函数的设计。

---

## 7. 策略插件框架（用户最关心的部分）

### 插件契约
每个策略 = `plugins/` 下一个 `.py` 文件，定义一个继承 `Strategy` 基类的子类：

```python
# plugins/momentum.py
from app.strategy.base import Strategy, RawSignal
from app.strategy.context import Context

class MomentumStrategy(Strategy):
    """20 日均线突破策略示例。"""
    
    name = "momentum"   # ← 必填，与 strategies.yaml 里的 strategy_id 对齐
    
    def run(self, ctx: Context, trade_date: str) -> list[RawSignal]:
        # ctx 提供:
        #   ctx.cash() → float                              # 当前虚拟现金
        #   ctx.position(symbol) → int                       # 当前虚拟持仓
        #   ctx.market(symbol, start, end, fields) → DataFrame
        #   ctx.universe(sector="沪深300") → list[str]
        # 返回: list[RawSignal]
        signals = []
        for symbol in ctx.universe("沪深300"):
            df = ctx.market(symbol, start=20260101, end=trade_date, fields=["close"])
            if len(df) < 20:
                continue
            ma20 = df["close"].rolling(20).mean().iloc[-1]
            today_close = df["close"].iloc[-1]
            if today_close > ma20 * 1.05:
                signals.append(RawSignal(
                    symbol=symbol,
                    direction="BUY",
                    quantity=100,
                    reference_price=today_close,
                    price_offset=0.005,
                ))
        return signals
```

### 加载机制
启动时 `loader.py` 扫描 `plugins/*.py` → 找出所有 `Strategy` 子类 → 按 `name` 注册。
`strategies.yaml` 里 `strategy_id` 必须匹配某个已注册策略，否则启动报错。

### 运算编排
`runner.py` 在行情入库后由 APScheduler 触发：
1. 读 `strategies.yaml` 列出所有 `(account_group, strategy_id)` 实例对
2. 对每个实例：
   - 构造 `Context`（注入实例对应的 virtual_cash + virtual_positions + 当日行情视图）
   - 调用对应策略的 `.run(ctx, trade_date)` 收集 `RawSignal[]`
3. 把所有 `RawSignal` 写入 `raw_signals` 表（precheck_status=NULL）
4. 触发 precheck → aggregate → orders 流水线

---

## 8. 测试策略

### 三层
- **unit/**：纯函数。无 I/O。Mock DB / Parquet / Strategy.run。
- **integration/**：触达真 SQLite (tmp file) + 真 Parquet (tmp dir)，但不起 HTTP。
- **e2e/**：FastAPI TestClient 全链路：HTTP → 业务 → DB → 验证响应。

### 覆盖率门槛
- services 层 ≥ 90%
- API 层 100%（每个端点至少 happy + auth-fail + 4xx + 5xx 4 个 case）
- strategy framework ≥ 85%

### 关键测试
- 同 `(account_group, symbol, direction, valid_date)` 下多策略输出归集为一条 order
- 成交回报按 signal_quantity 比例正确拆分（边界：余数处理）
- 鉴权失败立即 401，不消耗后续资源
- 重复推送 `/market-data` 同一日期返回 2001 不重复入库
- 策略 plugin 抛异常不应 crash 整个 runner

---

## 9. 部署目标

```
阿里云 ECS (Ubuntu 22.04+)
├── /opt/qmt-server/
│   ├── server/                 # git pull or rsync 来的 v2.3/server/
│   ├── venv/                   # python3.11 -m venv
│   ├── data/                   # Parquet 仓库（首次 rsync from client）
│   ├── logs/
│   └── pipeline-server.db
├── /etc/systemd/system/qmt-server.service
└── 安全组放行 8000 (或 nginx 反代到 80/443)
```

详见 plan 13。

---

## 10. 14 个 Plan 的依赖与执行顺序

| # | 名称 | 依赖 | 状态 |
|---|---|---|---|
| 00 | 总览（本文件） | — | ✓ |
| 01 | scaffold（pyproject + FastAPI bootstrap + settings + logging + healthz） | — | 待开始 |
| 02 | parquet storage layer | 01 | |
| 03 | sqlite + ORM models | 01 | |
| 04 | API skeleton + auth + 3 端点 stub | 01, 03 | |
| 05 | ingest（POST /market-data 完整实现） | 02, 04 | |
| 06 | strategy framework + plugin loader | 03 | |
| 07 | precheck | 03, 06 | |
| 08 | aggregate | 03, 07 | |
| 09 | orders_queue + GET /orders 完整 | 04, 08 | |
| 10 | settlement + POST /trade-result 完整 | 04, 09 | |
| 11 | perf NAV | 03, 10 | |
| 12 | scheduler 编排 | 05, 06-11 | |
| 13 | 部署到阿里云 + 切换 client base_url | all | |
| 14 | （可选）`shared/` 抽出共享 schema 给 client 复用 | 04 | |

每个 plan 内部按 v1 风格做 TDD 任务拆分（Write test → Confirm fail → Implement → Confirm pass → Commit）。

---

## 11. 不在范围内的事（v2.3 server 不做）

- 真实策略代码：本期只写示例 `_example_buy_and_hold.py`；真策略由用户后续投放
- 实时撮合 / 风控告警面板：先靠日志 + 微信报警
- 多机部署 / 主备 / 容灾：单机够用
- Docker 化：systemd 起步
- 用户认证体系：固定 API Key 即可（API spec 也只要求 Bearer）
- 前端 dashboard：暂用 Swagger UI
- WebSocket 推送：暂用 polling
