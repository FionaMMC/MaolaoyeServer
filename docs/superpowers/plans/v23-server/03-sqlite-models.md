# Plan 03: SQLite + ORM models (`app/db.py`, `app/models/`)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** SQLAlchemy 2.0 ORM 模型 + session factory + 建表函数。覆盖 6 张业务表（instance_state / raw_signals / orders / order_signal_map / trades / perf_snapshots）。是 plans 04-11 的共同底层依赖。

**Architecture:**
- 用 SQLAlchemy 2.0 declarative + `Mapped`/`mapped_column` 的现代写法
- `app/db.py` 提供 `engine` 单例 + `session_factory()` + `init_db(engine)` 建表
- 表设计跟 `00-overview.md` 第 5 节一致；JSON 列用 SQLAlchemy `JSON` 类型（SQLite 原生支持）
- 用 SQLite 单文件；Settings.db_url 决定路径（测试时用 in-memory 或 tmp_path）
- 暂不引入 alembic 迁移（schema 稳定后再上）

**Tech Stack:** SQLAlchemy 2.0, sqlite3 (stdlib), pydantic-settings (Settings)

**Files:**
- `v2.3/server/app/db.py` (NEW)
- `v2.3/server/app/models/__init__.py` (NEW，集中 export Base + 所有 model)
- `v2.3/server/app/models/instance_state.py` (NEW)
- `v2.3/server/app/models/raw_signal.py` (NEW)
- `v2.3/server/app/models/order.py` (NEW)
- `v2.3/server/app/models/order_signal_map.py` (NEW)
- `v2.3/server/app/models/trade.py` (NEW)
- `v2.3/server/app/models/perf_snapshot.py` (NEW)
- `v2.3/server/tests/unit/test_db.py` (NEW)

---

## Task 1: Base + db.py（engine + session_factory + init_db）

### Step 1: 写测试

`tests/unit/test_db.py`（先只测 db.py 部分；models 的 schema 测试在 Task 2）:

```python
"""tests/unit/test_db.py"""
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.db import init_db, make_engine, make_session_factory


def test_make_engine_with_sqlite_url(tmp_path: Path):
    db_url = f"sqlite:///{tmp_path}/t.db"
    engine = make_engine(db_url)
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


def test_make_engine_in_memory():
    engine = make_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


def test_init_db_creates_all_tables(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    expected = {
        "instance_state",
        "raw_signals",
        "orders",
        "order_signal_map",
        "trades",
        "perf_snapshots",
    }
    assert expected.issubset(tables), f"缺表: {expected - tables}"


def test_init_db_is_idempotent(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    init_db(engine)  # 第二次不应报错（CREATE TABLE IF NOT EXISTS 行为）
    inspector = inspect(engine)
    assert "orders" in inspector.get_table_names()


def test_session_factory_yields_working_session(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    SessionFactory = make_session_factory(engine)
    with SessionFactory() as s:
        result = s.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        names = {r[0] for r in result.fetchall()}
        assert "orders" in names
```

### Step 2: 实现 `app/db.py`

```python
"""SQLAlchemy 2.0 engine + session factory + 建表。"""
from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(db_url: str) -> Engine:
    """创建 engine。SQLite 用 check_same_thread=False 以支持多线程（FastAPI 默认）。"""
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    return create_engine(db_url, connect_args=connect_args, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(engine: Engine) -> None:
    """根据 ORM 模型建表（CREATE TABLE IF NOT EXISTS 语义）。"""
    # 延迟 import 避免循环依赖（models/* import Base from app.models）
    from app.models import Base
    Base.metadata.create_all(bind=engine)
```

---

## Task 2: ORM Models（6 张表 + Base）

`app/models/__init__.py`:

```python
"""SQLAlchemy ORM models 集中导出。"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


# 触发 model 注册到 Base.metadata
from app.models.instance_state import InstanceState  # noqa: E402, F401
from app.models.raw_signal import RawSignal  # noqa: E402, F401
from app.models.order import Order  # noqa: E402, F401
from app.models.order_signal_map import OrderSignalMap  # noqa: E402, F401
from app.models.trade import Trade  # noqa: E402, F401
from app.models.perf_snapshot import PerfSnapshot  # noqa: E402, F401

__all__ = [
    "Base",
    "InstanceState",
    "RawSignal",
    "Order",
    "OrderSignalMap",
    "Trade",
    "PerfSnapshot",
]
```

### `app/models/instance_state.py`

```python
"""策略实例虚拟账本：每个 (account_group, strategy_id) 一行。"""
from __future__ import annotations

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class InstanceState(Base):
    __tablename__ = "instance_state"

    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    virtual_cash: Mapped[float] = mapped_column(nullable=False)
    virtual_positions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_update: Mapped[str] = mapped_column(String, nullable=False)  # ISO 8601
```

### `app/models/raw_signal.py`

```python
"""原始信号：策略输出，预检前后状态都在此表。"""
from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class RawSignal(Base):
    __tablename__ = "raw_signals"

    signal_id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)  # BUY / SELL
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_price: Mapped[float] = mapped_column(Float, nullable=False)
    price_offset: Mapped[float] = mapped_column(Float, nullable=False)
    limit_price: Mapped[float] = mapped_column(Float, nullable=False)
    valid_date: Mapped[str] = mapped_column(String, nullable=False, index=True)
    signal_time: Mapped[str] = mapped_column(String, nullable=False)
    precheck_status: Mapped[str] = mapped_column(String, nullable=False)  # PASS / FAIL
    precheck_reason: Mapped[str | None] = mapped_column(String, nullable=True)
```

### `app/models/order.py`

```python
"""归集后的订单：本地 client 据此下 QMT 委托。"""
from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_group: Mapped[str] = mapped_column(String, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[float] = mapped_column(Float, nullable=False)
    valid_date: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)  # ISO 8601
```

### `app/models/order_signal_map.py`

```python
"""归集订单 → 原始信号映射（拆单按比例分摊用）。复合主键。"""
from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class OrderSignalMap(Base):
    __tablename__ = "order_signal_map"

    order_id: Mapped[str] = mapped_column(String, primary_key=True)
    signal_id: Mapped[str] = mapped_column(String, primary_key=True)
    signal_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
```

### `app/models/trade.py`

```python
"""成交回报。一个 order 可能对应多笔成交（但 client 已聚合，所以通常 1:1）。"""
from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_price: Mapped[float] = mapped_column(Float, nullable=False)
    filled_time: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[str] = mapped_column(String, nullable=False)  # ISO 8601
```

### `app/models/perf_snapshot.py`

```python
"""每日策略实例净值快照。复合主键 (instance_id, date)。"""
from __future__ import annotations

from sqlalchemy import JSON, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class PerfSnapshot(Base):
    __tablename__ = "perf_snapshots"

    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    date: Mapped[str] = mapped_column(String, primary_key=True)  # YYYYMMDD
    nav: Mapped[float] = mapped_column(Float, nullable=False)
    daily_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    positions_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
```

### Step 3: 跑测试

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest tests/unit/test_db.py -v   # 期望 5 PASS
pytest -v                          # 期望 23 total (9+9+5)
```

### Step 4: Commit

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/app/db.py \
        v2.3/server/app/models/ \
        v2.3/server/tests/unit/test_db.py
git commit -m "feat(server): add SQLAlchemy 2.0 ORM models (6 tables) + db.py"
```

---

## 收尾

- [ ] 5 个 db 测试 PASS
- [ ] 6 表全部建出来
- [ ] 1 commit

---

## 后续 plan

Plan 04: API skeleton + auth middleware + 3 端点 stub
