# Plan 04: API skeleton + auth + 3 端点 stub

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 把 API 文档定义的 3 个端点落地为可调用的 stub（带鉴权 + Pydantic 校验 + 标准响应包装）。stub 接受请求 + 返回占位响应。后续 plans (05/09/10) 替换 stub 为真实业务实现。

**Architecture:**
- `Authorization: Bearer {API_KEY}` 中间件（FastAPI dependency）
- 标准响应：`{code, message, data}`，错误码用 `app/exceptions.py` 集中管
- Pydantic v2 schemas：`app/schemas/{market_data, orders, trade_result, common}.py`
- 路由按端点拆文件：`app/api/{market_data, orders, trade_result}.py`
- 鉴权失败立即 401 + `code=1001`，不消耗后续资源
- DTO 校验失败 → FastAPI 自动 422，全局 exception handler 包成 `{code: 1002, ...}` 的标准响应

**Files:**
- `v2.3/server/app/exceptions.py` (NEW) — 错误码 enum + APIError
- `v2.3/server/app/auth.py` (NEW) — Bearer token dependency
- `v2.3/server/app/schemas/__init__.py` (NEW)
- `v2.3/server/app/schemas/common.py` (NEW) — `APIResponse[T]` 包装器
- `v2.3/server/app/schemas/market_data.py` (NEW)
- `v2.3/server/app/schemas/orders.py` (NEW)
- `v2.3/server/app/schemas/trade_result.py` (NEW)
- `v2.3/server/app/api/market_data.py` (NEW) — POST /market-data stub
- `v2.3/server/app/api/orders.py` (NEW) — GET /orders stub
- `v2.3/server/app/api/trade_result.py` (NEW) — POST /trade-result stub
- `v2.3/server/app/main.py` (MODIFY) — 注册 3 个新 router + 全局 exception handler
- `v2.3/server/tests/unit/test_auth.py` (NEW)
- `v2.3/server/tests/unit/test_api_market_data.py` (NEW)
- `v2.3/server/tests/unit/test_api_orders.py` (NEW)
- `v2.3/server/tests/unit/test_api_trade_result.py` (NEW)

---

## Task 1: exceptions + auth + common schema

### Step 1: 写测试 `tests/unit/test_auth.py`

```python
"""tests/unit/test_auth.py — Bearer token auth dependency"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import verify_api_key
from app.settings import Settings


def _build_test_app(api_key: str) -> TestClient:
    """构造一个带鉴权的最小 FastAPI app 用于测试。"""
    from fastapi import Depends
    from app.main import create_app

    settings = Settings(api_key=api_key, log_level="WARNING")
    app = create_app(settings_override=settings)

    @app.get("/_protected")
    async def protected(_: None = Depends(verify_api_key)):
        return {"ok": True}

    return TestClient(app)


def test_auth_missing_header_returns_401():
    client = _build_test_app("KEY_ABC")
    resp = client.get("/_protected")
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == 1001


def test_auth_wrong_key_returns_401():
    client = _build_test_app("KEY_ABC")
    resp = client.get("/_protected", headers={"Authorization": "Bearer WRONG"})
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == 1001


def test_auth_correct_key_returns_200():
    client = _build_test_app("KEY_ABC")
    resp = client.get("/_protected", headers={"Authorization": "Bearer KEY_ABC"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_auth_malformed_header_returns_401():
    client = _build_test_app("KEY_ABC")
    resp = client.get("/_protected", headers={"Authorization": "KEY_ABC"})  # 缺 Bearer 前缀
    assert resp.status_code == 401
```

### Step 2: 实现 `app/exceptions.py`

```python
"""业务错误码 + APIError 基类。"""
from __future__ import annotations

from enum import IntEnum


class ErrorCode(IntEnum):
    """与 API 文档错误码定义一致。"""
    OK                = 0
    AUTH_FAILED       = 1001
    BAD_REQUEST       = 1002
    DUPLICATE_DATE    = 2001
    EMPTY_DATA        = 2002
    NON_TRADING_DAY   = 3001
    STRATEGY_PENDING  = 3002
    NO_ORDERS_MATCHED = 4001
    INTERNAL_ERROR    = 5000


class APIError(Exception):
    """业务异常。FastAPI exception handler 会包装为标准响应。"""

    def __init__(self, code: ErrorCode, message: str, http_status: int = 200):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)
```

### Step 3: 实现 `app/auth.py`

```python
"""Bearer token 鉴权 dependency。"""
from __future__ import annotations

from fastapi import Depends, Header

from app.exceptions import APIError, ErrorCode
from app.settings import Settings, get_settings


async def verify_api_key(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """检查 Authorization: Bearer {API_KEY}。失败抛 APIError(1001)。"""
    if not settings.api_key:
        # 防呆：没配 api_key 时所有请求拒绝（避免误开放生产）
        raise APIError(ErrorCode.AUTH_FAILED, "server api_key 未配置", http_status=401)

    if not authorization or not authorization.startswith("Bearer "):
        raise APIError(ErrorCode.AUTH_FAILED, "缺少 Bearer token", http_status=401)

    provided = authorization[len("Bearer "):]
    if provided != settings.api_key:
        raise APIError(ErrorCode.AUTH_FAILED, "API key 不匹配", http_status=401)
```

### Step 4: 实现 `app/schemas/__init__.py`

```python
# 包标记
```

### Step 5: 实现 `app/schemas/common.py`

```python
"""通用响应包装：{code, message, data}。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

DataT = TypeVar("DataT")


class APIResponse(BaseModel, Generic[DataT]):
    """所有端点统一响应结构。"""
    code: int
    message: str = "ok"
    data: DataT | None = None
```

### Step 6: 修改 `app/main.py`，加全局异常处理

把现有 `app/main.py` 改成：

```python
"""FastAPI 入口：create_app + lifespan + router 注册。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import health, market_data, orders, trade_result
from app.exceptions import APIError, ErrorCode
from app.logging_setup import get_logger, setup_logging
from app.settings import Settings, get_settings


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    log = get_logger("app")
    log.info("server_starting", version="2.3.0")
    yield
    log.info("server_stopping")


def create_app(settings_override: Settings | None = None) -> FastAPI:
    if settings_override is not None:
        get_settings.cache_clear()

    settings = settings_override or get_settings()
    setup_logging(log_level=settings.log_level, json_output=settings.log_json)

    app = FastAPI(
        title="QMT Pipeline Server",
        version="2.3.0",
        lifespan=_lifespan,
    )

    # 全局异常处理
    @app.exception_handler(APIError)
    async def _api_error_handler(request: Request, exc: APIError):
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": int(exc.code), "message": exc.message, "data": None},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=200,  # API 文档要求统一 HTTP 200，错误用 code 字段表示
            content={
                "code": int(ErrorCode.BAD_REQUEST),
                "message": f"请求参数不合法: {exc.errors()}",
                "data": None,
            },
        )

    # 路由注册
    app.include_router(health.router, tags=["health"])
    app.include_router(market_data.router, tags=["market-data"])
    app.include_router(orders.router, tags=["orders"])
    app.include_router(trade_result.router, tags=["trade-result"])

    if settings_override is not None:
        app.dependency_overrides[get_settings] = lambda: settings_override

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
```

---

## Task 2: 3 个端点 stub + schemas + 各自单测

### Schemas

`app/schemas/market_data.py`:

```python
"""POST /market-data 请求/响应 schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class StockBar(BaseModel):
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    is_suspended: bool
    turnover_rate: float | None = None  # 可选字段


class IndexBar(BaseModel):
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    amount: float = 0.0


class ETFBar(StockBar):
    """ETF 同 stock schema。"""


class MarketDataRequest(BaseModel):
    trade_date: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")
    stocks: list[StockBar] = Field(default_factory=list)
    indexes: list[IndexBar] = Field(default_factory=list)
    etfs: list[ETFBar] = Field(default_factory=list)


class MarketDataReceived(BaseModel):
    stocks: int
    indexes: int
    etfs: int


class MarketDataResponseData(BaseModel):
    trade_date: str
    received: MarketDataReceived
    strategy_triggered: bool = True
```

`app/schemas/orders.py`:

```python
"""GET /orders 响应 schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class OrderItem(BaseModel):
    order_id: str
    account_group: str
    symbol: str
    direction: str = Field(pattern=r"^(BUY|SELL)$")
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    valid_date: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")


class OrdersResponseData(BaseModel):
    date: str
    orders: list[OrderItem]
```

`app/schemas/trade_result.py`:

```python
"""POST /trade-result 请求/响应 schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TradeResult(BaseModel):
    order_id: str
    filled_quantity: int = Field(ge=0)
    filled_price: float = Field(ge=0)
    filled_time: str | None = None
    status: str = Field(pattern=r"^(FILLED|PARTIAL|CANCELLED|REJECTED)$")


class TradeResultRequest(BaseModel):
    trade_date: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")
    results: list[TradeResult]


class TradeResultResponseData(BaseModel):
    trade_date: str
    matched_count: int
    unmatched_order_ids: list[str] = Field(default_factory=list)
```

### Stub endpoints

`app/api/market_data.py`:

```python
"""POST /market-data — stub。Plan 05 实现真实业务。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import verify_api_key
from app.schemas.common import APIResponse
from app.schemas.market_data import (
    MarketDataReceived,
    MarketDataRequest,
    MarketDataResponseData,
)

router = APIRouter()


@router.post(
    "/market-data",
    response_model=APIResponse[MarketDataResponseData],
    dependencies=[Depends(verify_api_key)],
)
async def push_market_data(req: MarketDataRequest):
    """[STUB] 接收 client 推送的当日行情。"""
    return APIResponse[MarketDataResponseData](
        code=0,
        message="ok (stub)",
        data=MarketDataResponseData(
            trade_date=req.trade_date,
            received=MarketDataReceived(
                stocks=len(req.stocks),
                indexes=len(req.indexes),
                etfs=len(req.etfs),
            ),
            strategy_triggered=False,  # stub 还没接策略
        ),
    )
```

`app/api/orders.py`:

```python
"""GET /orders — stub。Plan 09 实现真实业务。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.auth import verify_api_key
from app.schemas.common import APIResponse
from app.schemas.orders import OrdersResponseData

router = APIRouter()


@router.get(
    "/orders",
    response_model=APIResponse[OrdersResponseData],
    dependencies=[Depends(verify_api_key)],
)
async def get_orders(date: str = Query(min_length=8, max_length=8, pattern=r"^\d{8}$")):
    """[STUB] 返回指定日期的归集订单。"""
    return APIResponse[OrdersResponseData](
        code=0,
        message="ok (stub)",
        data=OrdersResponseData(date=date, orders=[]),
    )
```

`app/api/trade_result.py`:

```python
"""POST /trade-result — stub。Plan 10 实现真实业务。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import verify_api_key
from app.schemas.common import APIResponse
from app.schemas.trade_result import TradeResultRequest, TradeResultResponseData

router = APIRouter()


@router.post(
    "/trade-result",
    response_model=APIResponse[TradeResultResponseData],
    dependencies=[Depends(verify_api_key)],
)
async def push_trade_result(req: TradeResultRequest):
    """[STUB] 接收成交回报。"""
    return APIResponse[TradeResultResponseData](
        code=0,
        message="ok (stub)",
        data=TradeResultResponseData(
            trade_date=req.trade_date,
            matched_count=0,
            unmatched_order_ids=[r.order_id for r in req.results],
        ),
    )
```

### Tests for the 3 endpoints

`tests/unit/test_api_market_data.py`:

```python
"""POST /market-data stub tests"""

_AUTH = {"Authorization": "Bearer TEST_KEY"}


def _payload():
    return {
        "trade_date": "20260430",
        "stocks": [{
            "symbol": "600519.SH",
            "open": 1500.0, "high": 1520.0, "low": 1490.0, "close": 1510.0,
            "volume": 1000, "amount": 1510000.0,
            "is_suspended": False,
        }],
        "indexes": [],
        "etfs": [],
    }


def test_post_market_data_happy_path(client):
    r = client.post("/market-data", headers=_AUTH, json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["received"]["stocks"] == 1


def test_post_market_data_no_auth_returns_401(client):
    r = client.post("/market-data", json=_payload())
    assert r.status_code == 401
    assert r.json()["code"] == 1001


def test_post_market_data_bad_payload_returns_1002(client):
    r = client.post("/market-data", headers=_AUTH, json={"trade_date": "bad"})
    body = r.json()
    assert body["code"] == 1002


def test_post_market_data_empty_arrays_ok(client):
    r = client.post("/market-data", headers=_AUTH,
                    json={"trade_date": "20260430", "stocks": [],
                          "indexes": [], "etfs": []})
    assert r.json()["code"] == 0
```

`tests/unit/test_api_orders.py`:

```python
"""GET /orders stub tests"""

_AUTH = {"Authorization": "Bearer TEST_KEY"}


def test_get_orders_happy_path(client):
    r = client.get("/orders?date=20260430", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["date"] == "20260430"
    assert body["data"]["orders"] == []


def test_get_orders_no_auth_returns_401(client):
    r = client.get("/orders?date=20260430")
    assert r.status_code == 401


def test_get_orders_missing_date_returns_1002(client):
    r = client.get("/orders", headers=_AUTH)
    assert r.json()["code"] == 1002


def test_get_orders_bad_date_returns_1002(client):
    r = client.get("/orders?date=2026-04-30", headers=_AUTH)  # 错误格式
    assert r.json()["code"] == 1002
```

`tests/unit/test_api_trade_result.py`:

```python
"""POST /trade-result stub tests"""

_AUTH = {"Authorization": "Bearer TEST_KEY"}


def _payload():
    return {
        "trade_date": "20260430",
        "results": [
            {"order_id": "o1", "filled_quantity": 100, "filled_price": 10.5,
             "filled_time": "2026-04-30T09:25:00+08:00", "status": "FILLED"},
        ],
    }


def test_post_trade_result_happy_path(client):
    r = client.post("/trade-result", headers=_AUTH, json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["trade_date"] == "20260430"


def test_post_trade_result_no_auth_returns_401(client):
    r = client.post("/trade-result", json=_payload())
    assert r.status_code == 401


def test_post_trade_result_bad_status_returns_1002(client):
    bad = _payload()
    bad["results"][0]["status"] = "WHATEVER"
    r = client.post("/trade-result", headers=_AUTH, json=bad)
    assert r.json()["code"] == 1002


def test_post_trade_result_empty_results_ok(client):
    r = client.post("/trade-result", headers=_AUTH,
                    json={"trade_date": "20260430", "results": []})
    assert r.json()["code"] == 0
```

### Verify + commit

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest -v   # expect: 23 prior + 4 auth + 4 market_data + 4 orders + 4 trade_result = 39

cd /Users/mameican/Desktop/server
git add v2.3/server/app/exceptions.py v2.3/server/app/auth.py \
        v2.3/server/app/schemas/ \
        v2.3/server/app/api/market_data.py v2.3/server/app/api/orders.py v2.3/server/app/api/trade_result.py \
        v2.3/server/app/main.py \
        v2.3/server/tests/unit/test_auth.py \
        v2.3/server/tests/unit/test_api_market_data.py \
        v2.3/server/tests/unit/test_api_orders.py \
        v2.3/server/tests/unit/test_api_trade_result.py
git commit -m "feat(server): add API skeleton with auth + 3 endpoint stubs (Plan 04)"
```

---

## 收尾

- [ ] 16 个新测试 PASS（4 auth + 4*3 endpoints）
- [ ] `pytest -v` 全绿（39 总计）
- [ ] 1 commit

---

## 后续 plan

Plan 05: ingest（POST /market-data 真实业务实现，把行情入 Parquet）
