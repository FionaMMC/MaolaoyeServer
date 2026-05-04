# Plan 09: Orders Queue + GET /orders 真实业务

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 把 `GET /orders` 从 stub 升级为真实业务：读 SQLite `orders` 表 PENDING 状态记录、按 `valid_date` 过滤、转 `OrderItem` 返回。同时提供 `write_aggregated()` 给 Plan 12 调度器在归集后写入。

**里程碑：完成后 client 19:00 拉取订单可对接真实 server。**

**Architecture:**
- `OrdersQueueService`：包含 `write_aggregated()` + `list_pending(valid_date)` + `mark_status(order_id, status)` 三个方法
- `app/dependencies.py` 加 `get_db_session_factory` 和 `get_orders_queue_service`
- `GET /orders` 改用 OrdersQueueService

**Files:**
- `v2.3/server/app/services/orders_queue.py` (NEW)
- `v2.3/server/app/dependencies.py` (MODIFY，加 db session + service factory)
- `v2.3/server/app/main.py` (MODIFY，lifespan 里 init_db + 用 settings 建 engine)
- `v2.3/server/app/api/orders.py` (MODIFY，调 service)
- `v2.3/server/tests/conftest.py` (MODIFY，client fixture 提供 init 后的 DB)
- `v2.3/server/tests/unit/test_orders_queue.py` (NEW)
- `v2.3/server/tests/unit/test_api_orders.py` (MODIFY，补充端到端测试)

---

## Task 1: OrdersQueueService + 单测

### `app/services/orders_queue.py`

```python
"""订单队列：写归集结果 + 读 PENDING + 更新状态。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order, OrderSignalMap
from app.schemas.orders import OrderItem
from app.services.aggregate import AggregatedOrder, OrderSignalMapping


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class OrdersQueueService:
    """订单队列服务。每次调用通过 session_factory 拿独立 session。"""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    # ── 写：Plan 12 调度器在归集后调用 ─────────────────────────────────
    def write_aggregated(
        self,
        orders: list[AggregatedOrder],
        mappings: Iterable[OrderSignalMapping],
    ) -> int:
        """写入归集订单 + 订单→信号映射。返回新增 order 数。"""
        if not orders:
            return 0

        now = _now_iso()
        with self.session_factory() as session:
            for o in orders:
                session.add(Order(
                    order_id=o.order_id,
                    account_group=o.account_group,
                    symbol=o.symbol,
                    direction=o.direction,
                    quantity=o.quantity,
                    limit_price=o.limit_price,
                    valid_date=o.valid_date,
                    status="PENDING",
                    created_at=now,
                ))
            for m in mappings:
                session.add(OrderSignalMap(
                    order_id=m.order_id,
                    signal_id=m.signal_id,
                    signal_quantity=m.signal_quantity,
                ))
            session.commit()
        return len(orders)

    # ── 读：GET /orders 调用 ──────────────────────────────────────────
    def list_pending(self, valid_date: str) -> list[OrderItem]:
        """返回指定 valid_date 的所有 PENDING 订单（OrderItem schema）。"""
        with self.session_factory() as session:
            stmt = (
                select(Order)
                .where(Order.valid_date == valid_date)
                .where(Order.status == "PENDING")
                .order_by(Order.created_at)
            )
            rows = session.execute(stmt).scalars().all()
            return [
                OrderItem(
                    order_id=r.order_id,
                    account_group=r.account_group,
                    symbol=r.symbol,
                    direction=r.direction,
                    quantity=r.quantity,
                    limit_price=r.limit_price,
                    valid_date=r.valid_date,
                )
                for r in rows
            ]

    # ── 写：Plan 10 settlement 调用 ───────────────────────────────────
    def mark_status(self, order_id: str, status: str) -> bool:
        """更新订单状态。返回是否找到对应 order。"""
        with self.session_factory() as session:
            order = session.get(Order, order_id)
            if order is None:
                return False
            order.status = status
            session.commit()
            return True
```

### `tests/unit/test_orders_queue.py`

```python
"""OrdersQueueService 单元测试"""
from pathlib import Path

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import Order, OrderSignalMap
from app.services.aggregate import AggregatedOrder, OrderSignalMapping
from app.services.orders_queue import OrdersQueueService


def _factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    return make_session_factory(engine)


def _ord(order_id: str = "o1", **overrides) -> AggregatedOrder:
    base = dict(
        order_id=order_id, account_group="real_A", symbol="600519.SH",
        direction="BUY", quantity=300, limit_price=10.05, valid_date="20260430",
    )
    base.update(overrides)
    return AggregatedOrder(**base)


def test_write_aggregated_persists_orders_and_mappings(tmp_path: Path):
    f = _factory(tmp_path)
    svc = OrdersQueueService(session_factory=f)
    n = svc.write_aggregated(
        orders=[_ord(order_id="oid1")],
        mappings=[
            OrderSignalMapping(order_id="oid1", signal_id="s1", signal_quantity=100),
            OrderSignalMapping(order_id="oid1", signal_id="s2", signal_quantity=200),
        ],
    )
    assert n == 1
    with f() as s:
        assert s.get(Order, "oid1").status == "PENDING"
        maps = s.query(OrderSignalMap).filter_by(order_id="oid1").all()
        assert {m.signal_id for m in maps} == {"s1", "s2"}


def test_write_aggregated_empty_returns_zero(tmp_path: Path):
    svc = OrdersQueueService(session_factory=_factory(tmp_path))
    assert svc.write_aggregated([], []) == 0


def test_list_pending_returns_only_matching_date(tmp_path: Path):
    svc = OrdersQueueService(session_factory=_factory(tmp_path))
    svc.write_aggregated([
        _ord(order_id="o1", valid_date="20260430"),
        _ord(order_id="o2", valid_date="20260501"),
    ], [])
    items = svc.list_pending("20260430")
    assert [i.order_id for i in items] == ["o1"]


def test_list_pending_excludes_non_pending(tmp_path: Path):
    f = _factory(tmp_path)
    svc = OrdersQueueService(session_factory=f)
    svc.write_aggregated([
        _ord(order_id="o1"),
        _ord(order_id="o2"),
    ], [])
    # 把 o1 标记为 FILLED
    with f() as s:
        s.get(Order, "o1").status = "FILLED"
        s.commit()
    items = svc.list_pending("20260430")
    assert [i.order_id for i in items] == ["o2"]


def test_list_pending_empty_when_no_orders(tmp_path: Path):
    svc = OrdersQueueService(session_factory=_factory(tmp_path))
    assert svc.list_pending("20260430") == []


def test_mark_status_updates(tmp_path: Path):
    f = _factory(tmp_path)
    svc = OrdersQueueService(session_factory=f)
    svc.write_aggregated([_ord(order_id="o1")], [])

    assert svc.mark_status("o1", "FILLED") is True
    with f() as s:
        assert s.get(Order, "o1").status == "FILLED"


def test_mark_status_unknown_returns_false(tmp_path: Path):
    svc = OrdersQueueService(session_factory=_factory(tmp_path))
    assert svc.mark_status("nope", "FILLED") is False
```

---

## Task 2: 加 DB session 注入到 FastAPI + 改 GET /orders endpoint

### `app/dependencies.py` —— 改成

```python
"""共享 Depends 工厂：Settings → Engine → SessionFactory → Services。"""
from __future__ import annotations

from functools import lru_cache

from fastapi import Depends
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.db import init_db, make_engine, make_session_factory
from app.services.ingest import IngestService
from app.services.orders_queue import OrdersQueueService
from app.settings import Settings, get_settings
from app.storage.parquet import ParquetStore


# ── Engine + Session（按 db_url 缓存，整个进程一份） ──────────────────
@lru_cache(maxsize=8)
def _engine_for_url(db_url: str) -> Engine:
    eng = make_engine(db_url)
    init_db(eng)   # 首次访问就建表（CREATE TABLE IF NOT EXISTS 幂等）
    return eng


def get_engine(settings: Settings = Depends(get_settings)) -> Engine:
    return _engine_for_url(settings.db_url)


def get_session_factory(engine: Engine = Depends(get_engine)) -> sessionmaker:
    return make_session_factory(engine)


# ── ParquetStore ──────────────────────────────────────────────────────
def get_parquet_store(settings: Settings = Depends(get_settings)) -> ParquetStore:
    return ParquetStore(root=settings.parquet_root)


# ── Services ──────────────────────────────────────────────────────────
def get_ingest_service(
    store: ParquetStore = Depends(get_parquet_store),
) -> IngestService:
    return IngestService(parquet_store=store)


def get_orders_queue_service(
    sf: sessionmaker = Depends(get_session_factory),
) -> OrdersQueueService:
    return OrdersQueueService(session_factory=sf)
```

### `app/api/orders.py` —— 替换 stub

```python
"""GET /orders — 真实业务：从 SQLite 读取 PENDING 订单。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.auth import verify_api_key
from app.dependencies import get_orders_queue_service
from app.schemas.common import APIResponse
from app.schemas.orders import OrdersResponseData
from app.services.orders_queue import OrdersQueueService

router = APIRouter()


@router.get(
    "/orders",
    response_model=APIResponse[OrdersResponseData],
    dependencies=[Depends(verify_api_key)],
)
async def get_orders(
    date: str = Query(min_length=8, max_length=8, pattern=r"^\d{8}$"),
    service: OrdersQueueService = Depends(get_orders_queue_service),
):
    """返回指定交易日的 PENDING 归集订单。"""
    items = service.list_pending(valid_date=date)
    return APIResponse[OrdersResponseData](
        code=0,
        message="ok",
        data=OrdersResponseData(date=date, orders=items),
    )
```

### `tests/conftest.py` —— 改进 client fixture（保证 db 路径 init）

把现有的 `client` fixture 改成：

```python
@pytest.fixture
def client(settings_for_test, monkeypatch) -> TestClient:
    """FastAPI TestClient + override settings + 清空 _engine_for_url 缓存。"""
    from app.dependencies import _engine_for_url

    get_settings.cache_clear()
    _engine_for_url.cache_clear()   # 测试间隔离 engine 缓存
    app = create_app(settings_override=settings_for_test)
    return TestClient(app)
```

### `tests/unit/test_api_orders.py` —— 末尾追加端到端测试

```python
# 末尾追加（保留原 4 个测试）

def test_get_orders_returns_seeded_orders(client, settings_for_test):
    """e2e: 直接往 DB 里写一条订单，再 GET /orders 看能否返回。"""
    from datetime import datetime, timezone

    from app.db import make_engine, make_session_factory
    from app.dependencies import _engine_for_url
    from app.models import Order

    engine = _engine_for_url(settings_for_test.db_url)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(Order(
            order_id="test-oid-1",
            account_group="real_A",
            symbol="600519.SH",
            direction="BUY",
            quantity=300,
            limit_price=10.05,
            valid_date="20260430",
            status="PENDING",
            created_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        ))
        s.commit()

    r = client.get("/orders?date=20260430", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert len(body["data"]["orders"]) == 1
    o = body["data"]["orders"][0]
    assert o["order_id"] == "test-oid-1"
    assert o["account_group"] == "real_A"
    assert o["quantity"] == 300


def test_get_orders_filters_out_non_pending(client, settings_for_test):
    from datetime import datetime, timezone

    from app.db import make_session_factory
    from app.dependencies import _engine_for_url
    from app.models import Order

    engine = _engine_for_url(settings_for_test.db_url)
    sf = make_session_factory(engine)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with sf() as s:
        s.add(Order(order_id="pending-1", account_group="real_A",
                    symbol="A.SH", direction="BUY", quantity=100,
                    limit_price=1.0, valid_date="20260430",
                    status="PENDING", created_at=now))
        s.add(Order(order_id="filled-1", account_group="real_A",
                    symbol="B.SH", direction="BUY", quantity=100,
                    limit_price=1.0, valid_date="20260430",
                    status="FILLED", created_at=now))
        s.commit()

    r = client.get("/orders?date=20260430", headers=_AUTH)
    body = r.json()
    ids = {o["order_id"] for o in body["data"]["orders"]}
    assert ids == {"pending-1"}
```

---

## 验证 + Commit

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest -v   # 期望 101 + 7 service + 2 endpoint = 110
```

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/app/services/orders_queue.py \
        v2.3/server/app/dependencies.py \
        v2.3/server/app/api/orders.py \
        v2.3/server/tests/conftest.py \
        v2.3/server/tests/unit/test_orders_queue.py \
        v2.3/server/tests/unit/test_api_orders.py
git commit -m "feat(server): wire GET /orders to OrdersQueueService (Plan 09)"
```

---

## 收尾

- [ ] 110 PASS
- [ ] 1 commit

**里程碑达成**：3 个端点中 2 个真实可用（`POST /market-data` + `GET /orders`）。第 3 个 `POST /trade-result` 在 Plan 10。

---

## 后续 plan

Plan 10: settlement + POST /trade-result（第三个端点真实化）
