# 模块三：信号查询与制单 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每个交易日 19:00 从服务器 `GET /signals?date={next_trade_date}` 查询次日信号；校验 `valid_date` 后写入本地 SQLite `signals` 表作为次日下单来源；按《项目设计文档（纯股）》的三态逻辑分别发出微信通知或报警。

**Architecture:** 新增共享 `src/common/db.py`（SQLite 连接封装与 schema 初始化，D/E 模块复用）。模块三专属 `next_trading_day.py`（内联 xtquant 计算次日）、`fetcher.py`（GET + `code=3002` 重试逻辑）、`validator.py`（`valid_date` 校验）、`store.py`（写 signals 表）、`__main__.py`（CLI 编排）。服务器返回语义严格按 API 文档三态处理，不把"空信号"当报警。

**Tech Stack:** Python 3.11, httpx, sqlite3（stdlib），xtquant（仅 Windows，用于 next_trading_day）, pytest, httpx.MockTransport。

**前置:** Plan A（模块一基础设施）、Plan B（http_client + notify + 配置）已完成。本 Plan 复用：
- `src/common/config.py`、`src/common/http_client.py`、`src/common/notify.py`、`src/common/logging_setup.py`
- 模块一 `connector.py` 的 xtquant 初始化（启动时调用 `init_xtquant`）

---

## 文件结构

**新建（共享基础设施，D/E 复用）：**
- `/Users/mameican/Desktop/server/src/common/db.py`
- `/Users/mameican/Desktop/server/tests/common/test_db.py`

**新建（模块三专属）：**
- `/Users/mameican/Desktop/server/src/signal_query/__init__.py`
- `/Users/mameican/Desktop/server/src/signal_query/next_trading_day.py`
- `/Users/mameican/Desktop/server/src/signal_query/fetcher.py`
- `/Users/mameican/Desktop/server/src/signal_query/validator.py`
- `/Users/mameican/Desktop/server/src/signal_query/store.py`
- `/Users/mameican/Desktop/server/src/signal_query/__main__.py`
- `/Users/mameican/Desktop/server/tests/signal_query/__init__.py`
- `/Users/mameican/Desktop/server/tests/signal_query/test_next_trading_day.py`
- `/Users/mameican/Desktop/server/tests/signal_query/test_fetcher.py`
- `/Users/mameican/Desktop/server/tests/signal_query/test_validator.py`
- `/Users/mameican/Desktop/server/tests/signal_query/test_store.py`
- `/Users/mameican/Desktop/server/tests/signal_query/test_cli.py`

**新建（集成冒烟测试文档）：**
- `/Users/mameican/Desktop/server/docs/manual_tests/module3_signal_query_smoke_test.md`

---

## Task 1: 共享 SQLite 层（schema + 连接封装）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/common/db.py`
- Create: `/Users/mameican/Desktop/server/tests/common/test_db.py`

**产出:**
- `get_connection(path) -> sqlite3.Connection` — 返回启用外键约束的连接
- `init_schema(conn)` — 幂等创建 `signals`、`orders`、`trades` 三张表（字段与设计文档一致）

> **为什么一次把三张表都建出来:** schema 是一次性决策；模块三/四/五分别使用不同表，但提前落地 schema 让后续模块直接 INSERT 即可，无需二次 migration。未来增表再加 migration。

- [ ] **Step 1: 先写失败测试**

```python
"""src.common.db 测试"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.common.db import get_connection, init_schema


def test_get_connection_creates_file(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        assert db.exists()
    finally:
        conn.close()


def test_init_schema_creates_three_tables(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        init_schema(conn)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {r[0] for r in cur.fetchall()}
        assert {"signals", "orders", "trades"}.issubset(tables)
    finally:
        conn.close()


def test_init_schema_is_idempotent(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        init_schema(conn)
        init_schema(conn)  # 二次调用不应报错
        cur = conn.execute("SELECT COUNT(*) FROM signals")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_signals_table_has_expected_columns(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        init_schema(conn)
        cur = conn.execute("PRAGMA table_info(signals)")
        cols = {r[1] for r in cur.fetchall()}
        assert cols == {
            "signal_id", "symbol", "direction", "quantity", "order_type",
            "limit_price", "price_offset", "strategy_id",
            "signal_time", "valid_date", "fetched_at",
        }
    finally:
        conn.close()


def test_orders_table_has_expected_columns(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        init_schema(conn)
        cur = conn.execute("PRAGMA table_info(orders)")
        cols = {r[1] for r in cur.fetchall()}
        assert cols == {
            "order_id", "signal_id", "symbol", "direction",
            "submitted_price", "submitted_quantity", "submitted_at",
            "submit_status",
        }
    finally:
        conn.close()


def test_trades_table_has_expected_columns(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        init_schema(conn)
        cur = conn.execute("PRAGMA table_info(trades)")
        cols = {r[1] for r in cur.fetchall()}
        assert cols == {
            "id", "order_id", "signal_id",
            "filled_quantity", "filled_price", "filled_time",
            "status", "reported_at", "report_status",
        }
    finally:
        conn.close()


def test_foreign_keys_enabled(tmp_path: Path):
    db = tmp_path / "trading.db"
    conn = get_connection(db)
    try:
        cur = conn.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
source /Users/mameican/Desktop/server/venv/bin/activate
pytest tests/common/test_db.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `src/common/db.py`**

```python
"""SQLite 连接封装 + 三表 schema 初始化。

表结构对应《项目设计文档（纯股）》的 signals / orders / trades。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


_SIGNALS_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id     TEXT PRIMARY KEY,
    symbol        TEXT NOT NULL,
    direction     TEXT NOT NULL,
    quantity      INTEGER NOT NULL,
    order_type    TEXT NOT NULL,
    limit_price   REAL,
    price_offset  REAL NOT NULL,
    strategy_id   TEXT NOT NULL,
    signal_time   TEXT NOT NULL,
    valid_date    TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);
"""

_ORDERS_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    order_id            TEXT PRIMARY KEY,
    signal_id           TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    submitted_price     REAL NOT NULL,
    submitted_quantity  INTEGER NOT NULL,
    submitted_at        TEXT NOT NULL,
    submit_status       TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
);
"""

_TRADES_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id         TEXT NOT NULL,
    signal_id        TEXT NOT NULL,
    filled_quantity  INTEGER NOT NULL,
    filled_price     REAL NOT NULL,
    filled_time      TEXT,
    status           TEXT NOT NULL,
    reported_at      TEXT,
    report_status    TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
"""


def get_connection(path: Path | str) -> sqlite3.Connection:
    """返回启用外键的 SQLite 连接，文件不存在会自动创建。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等创建三张表。"""
    conn.execute(_SIGNALS_SQL)
    conn.execute(_ORDERS_SQL)
    conn.execute(_TRADES_SQL)
    conn.commit()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/common/test_db.py -v
```

预期：7 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/common/db.py tests/common/test_db.py
git commit -m "feat(common): add sqlite layer with signals/orders/trades schema"
```

---

## Task 2: 次一交易日计算（内联 xtquant）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/signal_query/__init__.py`
- Create: `/Users/mameican/Desktop/server/src/signal_query/next_trading_day.py`
- Create: `/Users/mameican/Desktop/server/tests/signal_query/__init__.py`
- Create: `/Users/mameican/Desktop/server/tests/signal_query/test_next_trading_day.py`

**产出:** `next_trading_day(today: str) -> str`，`today` 为 `YYYYMMDD`；返回 `today` 之后的第一个交易日。使用 `xtdata.get_trading_dates("SH", count=30)` 取近期交易日列表并找下一个。

- [ ] **Step 1: 写两个空 `__init__.py`**

```python
# 包标记
```

分别写到：
- `/Users/mameican/Desktop/server/src/signal_query/__init__.py`
- `/Users/mameican/Desktop/server/tests/signal_query/__init__.py`

- [ ] **Step 2: 先写失败测试**

```python
"""next_trading_day 测试：mock xtquant"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_xtdata(monkeypatch):
    fake = SimpleNamespace(
        data_dir="",
        get_trading_dates=MagicMock(return_value=[
            "20260420", "20260421", "20260422",
            "20260423", "20260424", "20260427",
        ]),
    )
    monkeypatch.setitem(sys.modules, "xtquant", SimpleNamespace(xtdata=fake))
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake)
    return fake


def test_next_trading_day_skips_weekend(fake_xtdata):
    from src.signal_query.next_trading_day import next_trading_day

    # 20260424 是周五，下一交易日是周一 20260427
    result = next_trading_day("20260424")
    assert result == "20260427"


def test_next_trading_day_next_day_is_trading(fake_xtdata):
    from src.signal_query.next_trading_day import next_trading_day

    result = next_trading_day("20260422")
    assert result == "20260423"


def test_next_trading_day_today_not_in_calendar_raises(fake_xtdata):
    """today 不在最近 30 天交易日内（例：非交易日输入）"""
    from src.signal_query.next_trading_day import next_trading_day

    with pytest.raises(ValueError, match="today"):
        next_trading_day("20260425")  # 周六，不在列表里


def test_next_trading_day_no_future_date_raises(fake_xtdata):
    """列表末尾：没有下一个交易日。"""
    from src.signal_query.next_trading_day import next_trading_day

    fake_xtdata.get_trading_dates.return_value = ["20260420", "20260421", "20260422"]

    with pytest.raises(RuntimeError, match="下一"):
        next_trading_day("20260422")
```

- [ ] **Step 3: 跑测试确认失败**

```bash
pytest tests/signal_query/test_next_trading_day.py -v
```

预期：ImportError。

- [ ] **Step 4: 实现 `next_trading_day.py`**

```python
"""内联的"下一交易日"计算，仅此处调用 xtquant.get_trading_dates。"""
from __future__ import annotations


def next_trading_day(today: str) -> str:
    """根据 xtquant 日历返回 today 之后的第一个交易日（YYYYMMDD）。

    Raises:
        ValueError: today 不在 xtquant 返回的交易日列表中
        RuntimeError: today 已是列表末尾，无下一个
    """
    from xtquant import xtdata

    dates = xtdata.get_trading_dates("SH", count=30)
    as_str = [str(d)[:8] if not isinstance(d, str) else d for d in dates]

    if today not in as_str:
        raise ValueError(f"today={today} 不在近期交易日列表（非交易日或超出 30 天窗口）")

    idx = as_str.index(today)
    if idx + 1 >= len(as_str):
        raise RuntimeError(
            f"today={today} 已是 xtquant 日历末尾，无下一交易日（请扩大 count）"
        )
    return as_str[idx + 1]
```

- [ ] **Step 5: 跑测试确认通过**

```bash
pytest tests/signal_query/test_next_trading_day.py -v
```

预期：4 个测试 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/signal_query/__init__.py src/signal_query/next_trading_day.py tests/signal_query/__init__.py tests/signal_query/test_next_trading_day.py
git commit -m "feat(signal-query): add next_trading_day helper via xtquant calendar"
```

---

## Task 3: Signal Fetcher（GET /signals 三态处理）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/signal_query/fetcher.py`
- Create: `/Users/mameican/Desktop/server/tests/signal_query/test_fetcher.py`

**服务器三态（参考《API接口文档（纯股）》接口二）:**

| HTTP 状态 | code | signals | 语义 | 本地动作 |
|---|---|---|---|---|
| 200 | 0 | 非空 | 策略完成，有信号 | 返回 `HAS_SIGNALS` |
| 200 | 0 | 空 | 策略完成，无信号 | 返回 `NO_SIGNALS` |
| 200 | 3002 | — | 策略未完成，可重试 | 等 `wait_secs` 后重试一次；仍 3002 则 `STILL_PENDING` |
| 其他 | 其他 | — | 异常 | `ERROR` |

**产出:** `fetch_signals(date, http_client, wait_secs=1800) -> FetchResult`

`FetchResult` dataclass：
- `state: Literal["HAS_SIGNALS", "NO_SIGNALS", "STILL_PENDING", "ERROR"]`
- `signals: list[dict]` — 服务器原始信号字典列表
- `error: str | None`

- [ ] **Step 1: 先写失败测试**

```python
"""fetcher 测试（用 httpx.MockTransport）"""
from __future__ import annotations

import httpx
import pytest

from src.common.http_client import new_http_client
from src.signal_query.fetcher import FetchResult, fetch_signals


def _signal(sid: str = "sig1", valid: str = "20260422") -> dict:
    return {
        "signal_id": sid,
        "symbol": "600519.SH",
        "direction": "BUY",
        "quantity": 100,
        "order_type": "LIMIT",
        "limit_price": 1540.0,
        "price_offset": 0.005,
        "strategy_id": "s1",
        "signal_time": "2026-04-21T18:30:00+08:00",
        "valid_date": valid,
    }


def _mk_client(handlers: list) -> httpx.Client:
    it = iter(handlers)

    def h(req: httpx.Request) -> httpx.Response:
        return next(it)(req)

    return new_http_client("https://srv", "K", timeout=10,
                           transport=httpx.MockTransport(h))


def test_fetch_has_signals():
    client = _mk_client([
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"date": "20260422", "signals": [_signal()]}}),
    ])

    r = fetch_signals("20260422", client, wait_secs=0)

    assert isinstance(r, FetchResult)
    assert r.state == "HAS_SIGNALS"
    assert len(r.signals) == 1
    assert r.signals[0]["signal_id"] == "sig1"


def test_fetch_empty_signals_returns_no_signals():
    client = _mk_client([
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"date": "20260422", "signals": []}}),
    ])

    r = fetch_signals("20260422", client, wait_secs=0)

    assert r.state == "NO_SIGNALS"
    assert r.signals == []


def test_fetch_3002_then_ok(monkeypatch):
    """第一次返回 3002，等待后重试成功"""
    client = _mk_client([
        lambda req: httpx.Response(200, json={"code": 3002, "message": "pending"}),
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"signals": [_signal()]}}),
    ])

    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    r = fetch_signals("20260422", client, wait_secs=1800)

    assert r.state == "HAS_SIGNALS"
    assert sleep_calls == [1800]


def test_fetch_3002_twice_returns_still_pending(monkeypatch):
    client = _mk_client([
        lambda req: httpx.Response(200, json={"code": 3002, "message": "pending"}),
        lambda req: httpx.Response(200, json={"code": 3002, "message": "pending"}),
    ])
    monkeypatch.setattr("time.sleep", lambda s: None)

    r = fetch_signals("20260422", client, wait_secs=0)

    assert r.state == "STILL_PENDING"
    assert r.error is not None and "3002" in r.error


def test_fetch_other_error_returns_error():
    client = _mk_client([
        lambda req: httpx.Response(200, json={"code": 1001, "message": "auth"}),
    ])

    r = fetch_signals("20260422", client, wait_secs=0)

    assert r.state == "ERROR"
    assert "1001" in (r.error or "")


def test_fetch_http_500_returns_error():
    client = _mk_client([
        lambda req: httpx.Response(500, text="boom"),
    ])

    r = fetch_signals("20260422", client, wait_secs=0)

    assert r.state == "ERROR"
    assert "500" in (r.error or "")


def test_fetch_network_error_returns_error():
    def handler(req):
        raise httpx.ConnectError("no route")

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))

    r = fetch_signals("20260422", client, wait_secs=0)

    assert r.state == "ERROR"


def test_fetch_uses_query_param_date():
    captured = {}

    def handler(req):
        captured["url"] = str(req.url)
        return httpx.Response(200, json={"code": 0, "data": {"signals": []}})

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))

    fetch_signals("20260422", client, wait_secs=0)

    assert "date=20260422" in captured["url"]
    assert "/signals" in captured["url"]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/signal_query/test_fetcher.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `fetcher.py`**

```python
"""GET /signals 三态处理"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

FetchState = Literal["HAS_SIGNALS", "NO_SIGNALS", "STILL_PENDING", "ERROR"]


@dataclass
class FetchResult:
    state: FetchState
    signals: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def _parse_response(resp: httpx.Response) -> tuple[str, list[dict], str | None]:
    """将 HTTP 响应解析为 (state, signals, error)。state 只可能是
    HAS_SIGNALS / NO_SIGNALS / PENDING / ERROR。"""
    if resp.status_code >= 500:
        return ("ERROR", [], f"HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        return ("ERROR", [], f"response not JSON: {e}")

    code = body.get("code")
    if code == 3002:
        return ("PENDING", [], f"code=3002 {body.get('message')}")
    if code != 0:
        return ("ERROR", [], f"code={code} message={body.get('message')}")

    signals = (body.get("data") or {}).get("signals") or []
    if not signals:
        return ("NO_SIGNALS", [], None)
    return ("HAS_SIGNALS", signals, None)


def fetch_signals(
    date: str,
    http_client: httpx.Client,
    wait_secs: int = 1800,
) -> FetchResult:
    """查询 /signals?date=X。PENDING 时等待 wait_secs 后重试一次。"""
    try:
        resp = http_client.get("/signals", params={"date": date})
    except httpx.HTTPError as e:
        return FetchResult(state="ERROR", error=f"network error: {e}")

    state, signals, err = _parse_response(resp)
    if state in ("HAS_SIGNALS", "NO_SIGNALS", "ERROR"):
        logger.info("fetch_signals 首次 state=%s", state)
        return FetchResult(state=state, signals=signals, error=err)

    # PENDING：等待后重试一次
    logger.info("服务器策略未完成（3002），等待 %ds 后重试一次", wait_secs)
    time.sleep(wait_secs)

    try:
        resp2 = http_client.get("/signals", params={"date": date})
    except httpx.HTTPError as e:
        return FetchResult(state="ERROR", error=f"retry network error: {e}")

    state2, signals2, err2 = _parse_response(resp2)
    if state2 == "PENDING":
        return FetchResult(state="STILL_PENDING",
                           error=f"重试后仍 3002: {err2}")
    if state2 == "ERROR":
        return FetchResult(state="ERROR", error=f"重试后错误: {err2}")
    return FetchResult(state=state2, signals=signals2, error=err2)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/signal_query/test_fetcher.py -v
```

预期：8 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/signal_query/fetcher.py tests/signal_query/test_fetcher.py
git commit -m "feat(signal-query): add fetcher with 3002-retry-once state machine"
```

---

## Task 4: 信号校验（`valid_date` 必须匹配）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/signal_query/validator.py`
- Create: `/Users/mameican/Desktop/server/tests/signal_query/test_validator.py`

**规则（参考 API 文档）:**
- `valid_date` 必须等于期望的 `next_trade_date`，否则标记为过期信号，不写入
- 必填字段缺失的信号视为非法，跳过
- 返回 `(valid_signals, rejected_signals)` 供上层通知用

- [ ] **Step 1: 先写失败测试**

```python
"""validator 测试"""
from __future__ import annotations

from src.signal_query.validator import validate_signals


def _s(valid_date="20260422", **overrides) -> dict:
    d = {
        "signal_id": "sig1",
        "symbol": "600519.SH",
        "direction": "BUY",
        "quantity": 100,
        "order_type": "LIMIT",
        "limit_price": 1540.0,
        "price_offset": 0.005,
        "strategy_id": "s1",
        "signal_time": "2026-04-21T18:30:00+08:00",
        "valid_date": valid_date,
    }
    d.update(overrides)
    return d


def test_validate_all_pass():
    sigs = [_s(signal_id="a"), _s(signal_id="b")]
    valid, rejected = validate_signals(sigs, expected_date="20260422")
    assert len(valid) == 2
    assert rejected == []


def test_validate_rejects_mismatched_valid_date():
    sigs = [_s(signal_id="a", valid_date="20260423")]
    valid, rejected = validate_signals(sigs, expected_date="20260422")
    assert valid == []
    assert len(rejected) == 1
    assert rejected[0]["signal_id"] == "a"
    assert rejected[0]["_reason"].startswith("valid_date")


def test_validate_rejects_missing_field():
    sig = _s(signal_id="a")
    del sig["symbol"]
    valid, rejected = validate_signals([sig], expected_date="20260422")
    assert valid == []
    assert rejected[0]["_reason"].startswith("missing")


def test_validate_rejects_bad_direction():
    sigs = [_s(signal_id="a", direction="HOLD")]
    valid, rejected = validate_signals(sigs, expected_date="20260422")
    assert valid == []
    assert "direction" in rejected[0]["_reason"]


def test_validate_rejects_bad_order_type():
    sigs = [_s(signal_id="a", order_type="STOP")]
    valid, rejected = validate_signals(sigs, expected_date="20260422")
    assert valid == []
    assert "order_type" in rejected[0]["_reason"]


def test_validate_mixed_some_valid():
    sigs = [
        _s(signal_id="good", valid_date="20260422"),
        _s(signal_id="bad", valid_date="20260425"),
    ]
    valid, rejected = validate_signals(sigs, expected_date="20260422")
    assert [s["signal_id"] for s in valid] == ["good"]
    assert [s["signal_id"] for s in rejected] == ["bad"]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/signal_query/test_validator.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `validator.py`**

```python
"""信号校验：valid_date、必填字段、枚举值。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_REQUIRED = {
    "signal_id", "symbol", "direction", "quantity", "order_type",
    "price_offset", "strategy_id", "signal_time", "valid_date",
}
_DIRECTIONS = {"BUY", "SELL"}
_ORDER_TYPES = {"LIMIT", "MARKET"}


def validate_signals(
    signals: list[dict[str, Any]],
    expected_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (valid, rejected)。rejected 每条多一个 `_reason` 字段便于上报。"""
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for s in signals:
        missing = _REQUIRED - set(s.keys())
        if missing:
            rejected.append({**s, "_reason": f"missing fields: {sorted(missing)}"})
            continue
        if s["valid_date"] != expected_date:
            rejected.append({**s,
                             "_reason": f"valid_date {s['valid_date']} != "
                                        f"expected {expected_date}"})
            continue
        if s["direction"] not in _DIRECTIONS:
            rejected.append({**s, "_reason": f"bad direction: {s['direction']}"})
            continue
        if s["order_type"] not in _ORDER_TYPES:
            rejected.append({**s, "_reason": f"bad order_type: {s['order_type']}"})
            continue
        valid.append(s)

    if rejected:
        logger.warning("校验拒绝 %d 条信号：%s",
                       len(rejected),
                       [(r["signal_id"], r["_reason"]) for r in rejected
                        if "signal_id" in r])
    return valid, rejected
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/signal_query/test_validator.py -v
```

预期：6 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/signal_query/validator.py tests/signal_query/test_validator.py
git commit -m "feat(signal-query): add signal validator (valid_date, required, enums)"
```

---

## Task 5: Signal Store（写 SQLite signals 表）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/signal_query/store.py`
- Create: `/Users/mameican/Desktop/server/tests/signal_query/test_store.py`

**行为:**
- 用 `INSERT OR REPLACE` 按 `signal_id` upsert
- 写入时补 `fetched_at = datetime.now(tz).isoformat()`
- 返回写入/覆盖的条数

- [ ] **Step 1: 先写失败测试**

```python
"""store 测试"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.db import get_connection, init_schema
from src.signal_query.store import save_signals


def _sig(sid: str = "a", valid: str = "20260422") -> dict:
    return {
        "signal_id": sid,
        "symbol": "600519.SH",
        "direction": "BUY",
        "quantity": 100,
        "order_type": "LIMIT",
        "limit_price": 1540.0,
        "price_offset": 0.005,
        "strategy_id": "s1",
        "signal_time": "2026-04-21T18:30:00+08:00",
        "valid_date": valid,
    }


def test_save_signals_inserts_rows(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = get_connection(db)
    init_schema(conn)

    n = save_signals(conn, [_sig("a"), _sig("b")])
    assert n == 2

    cur = conn.execute("SELECT signal_id, symbol, direction FROM signals ORDER BY signal_id")
    rows = cur.fetchall()
    assert [r[0] for r in rows] == ["a", "b"]
    assert rows[0][1] == "600519.SH"
    conn.close()


def test_save_signals_fills_fetched_at(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = get_connection(db)
    init_schema(conn)

    save_signals(conn, [_sig("a")])

    cur = conn.execute("SELECT fetched_at FROM signals WHERE signal_id='a'")
    fetched = cur.fetchone()[0]
    assert fetched  # 非空字符串
    assert "T" in fetched  # ISO 8601
    conn.close()


def test_save_signals_upsert_overwrites_existing(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = get_connection(db)
    init_schema(conn)

    save_signals(conn, [_sig("a")])
    # 第二次同 id 改 quantity
    s2 = _sig("a")
    s2["quantity"] = 999
    save_signals(conn, [s2])

    cur = conn.execute("SELECT quantity FROM signals WHERE signal_id='a'")
    assert cur.fetchone()[0] == 999
    cur2 = conn.execute("SELECT COUNT(*) FROM signals")
    assert cur2.fetchone()[0] == 1
    conn.close()


def test_save_signals_empty_list_returns_zero(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = get_connection(db)
    init_schema(conn)

    n = save_signals(conn, [])
    assert n == 0
    conn.close()


def test_save_signals_null_limit_price_for_market(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = get_connection(db)
    init_schema(conn)

    sig = _sig("a")
    sig["order_type"] = "MARKET"
    sig["limit_price"] = None
    save_signals(conn, [sig])

    cur = conn.execute("SELECT limit_price FROM signals WHERE signal_id='a'")
    assert cur.fetchone()[0] is None
    conn.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/signal_query/test_store.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `store.py`**

```python
"""写入 signals 表。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT OR REPLACE INTO signals
(signal_id, symbol, direction, quantity, order_type, limit_price,
 price_offset, strategy_id, signal_time, valid_date, fetched_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def save_signals(
    conn: sqlite3.Connection,
    signals: list[dict[str, Any]],
) -> int:
    """按 signal_id upsert 到 signals 表。"""
    if not signals:
        return 0

    fetched_at = _now_iso()
    rows = [
        (
            s["signal_id"], s["symbol"], s["direction"],
            int(s["quantity"]), s["order_type"],
            s.get("limit_price"),  # 市价单可能为 None
            float(s["price_offset"]), s["strategy_id"],
            s["signal_time"], s["valid_date"],
            fetched_at,
        )
        for s in signals
    ]
    conn.executemany(_UPSERT_SQL, rows)
    conn.commit()
    logger.info("signals 表 upsert %d 行，fetched_at=%s", len(rows), fetched_at)
    return len(rows)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/signal_query/test_store.py -v
```

预期：5 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/signal_query/store.py tests/signal_query/test_store.py
git commit -m "feat(signal-query): add upsert store for signals table"
```

---

## Task 6: CLI 编排（next_date → fetch → validate → store → notify）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/signal_query/__main__.py`
- Create: `/Users/mameican/Desktop/server/tests/signal_query/test_cli.py`

**用法:**

```bash
python -m src.signal_query --today 20260421 --config config/settings.yaml
```

`--today` 可选，缺省取当前系统日期转 `YYYYMMDD`。

**退出码:**
- `0` 有信号或无信号（均属正常）
- `1` 配置/参数错误
- `2` 下一交易日计算失败（xtquant 异常或日历无下一个）
- `3` 服务器错误（state=ERROR 或 STILL_PENDING）

**通知语义（按设计文档）:**
- HAS_SIGNALS：`"已制单，共 N 条信号"` info
- NO_SIGNALS：`"今日无交易信号"` info
- STILL_PENDING：`"策略未完成，重试后仍 3002"` alert
- ERROR：`"信号查询失败: ..."` alert

- [ ] **Step 1: 先写失败测试**

```python
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest


def _write_cfg(tmp: Path, data_root: str) -> Path:
    p = tmp / "settings.yaml"
    p.write_text(f"""
qmt:
  data_dir: "/tmp/fake_qmt"
  account_id: "ACC"
server:
  base_url: "https://srv"
  api_key: "KEY"
  timeout: 10
paths:
  data_root: "{data_root}"
  log_dir: "{data_root}/logs"
  sqlite_path: "{data_root}/trading.db"
notify:
  wecom_webhook: "https://wecom.example.com"
market_data:
  sector_name: "沪深A股"
""", encoding="utf-8")
    return p


@pytest.fixture
def fake_xtdata(monkeypatch):
    fake = SimpleNamespace(
        data_dir="",
        get_trading_dates=MagicMock(return_value=[
            "20260420", "20260421", "20260422", "20260423",
        ]),
    )
    monkeypatch.setitem(sys.modules, "xtquant", SimpleNamespace(xtdata=fake))
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake)
    return fake


def test_cli_has_signals(fake_xtdata, tmp_path: Path, monkeypatch):
    from src.signal_query import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))

    wecom_calls = []

    def server_handler(req):
        return httpx.Response(200, json={
            "code": 0, "data": {"date": "20260422", "signals": [{
                "signal_id": "s1", "symbol": "600519.SH",
                "direction": "BUY", "quantity": 100, "order_type": "LIMIT",
                "limit_price": 1540.0, "price_offset": 0.005,
                "strategy_id": "x", "signal_time": "2026-04-21T18:30:00+08:00",
                "valid_date": "20260422",
            }]}})

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_new_server_client",
                        lambda cfg: cli_mod.new_http_client(
                            cfg.server.base_url, cfg.server.api_key,
                            cfg.server.timeout,
                            transport=httpx.MockTransport(server_handler)))
    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))

    exit_code = cli_mod.main(["--today", "20260421", "--config", str(cfg)])

    assert exit_code == 0
    assert any("已制单" in c["text"]["content"] for c in wecom_calls)


def test_cli_no_signals(fake_xtdata, tmp_path: Path, monkeypatch):
    from src.signal_query import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))

    wecom_calls = []

    def server_handler(req):
        return httpx.Response(200, json={"code": 0,
                                         "data": {"signals": []}})

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_new_server_client",
                        lambda cfg: cli_mod.new_http_client(
                            cfg.server.base_url, cfg.server.api_key,
                            cfg.server.timeout,
                            transport=httpx.MockTransport(server_handler)))
    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))

    exit_code = cli_mod.main(["--today", "20260421", "--config", str(cfg)])

    assert exit_code == 0
    assert any("无交易信号" in c["text"]["content"] for c in wecom_calls)
    assert all(not c["text"]["content"].startswith("[报警]") for c in wecom_calls)


def test_cli_still_pending_alerts(fake_xtdata, tmp_path: Path, monkeypatch):
    from src.signal_query import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))

    wecom_calls = []

    def server_handler(req):
        return httpx.Response(200, json={"code": 3002, "message": "pending"})

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_new_server_client",
                        lambda cfg: cli_mod.new_http_client(
                            cfg.server.base_url, cfg.server.api_key,
                            cfg.server.timeout,
                            transport=httpx.MockTransport(server_handler)))
    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))
    monkeypatch.setattr("time.sleep", lambda s: None)

    exit_code = cli_mod.main([
        "--today", "20260421", "--config", str(cfg), "--wait-secs", "0",
    ])

    assert exit_code == 3
    assert any(c["text"]["content"].startswith("[报警]") for c in wecom_calls)


def test_cli_missing_config(tmp_path: Path):
    from src.signal_query import __main__ as cli_mod
    exit_code = cli_mod.main(["--today", "20260421",
                              "--config", str(tmp_path / "nope.yaml")])
    assert exit_code == 1
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/signal_query/test_cli.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `__main__.py`**

```python
"""CLI：python -m src.signal_query --today YYYYMMDD --config path"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from src.common.config import Config, load_config
from src.common.db import get_connection, init_schema
from src.common.http_client import new_http_client
from src.common.logging_setup import setup_logging
from src.common.notify import notify_wecom
from src.market_data_download.connector import init_xtquant
from src.signal_query.fetcher import fetch_signals
from src.signal_query.next_trading_day import next_trading_day
from src.signal_query.store import save_signals
from src.signal_query.validator import validate_signals


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.signal_query",
        description="查询服务器信号、校验、写入 SQLite、通知。",
    )
    p.add_argument("--today", help="YYYYMMDD；缺省取本机当天")
    p.add_argument("--config", required=True, help="settings.yaml 路径")
    p.add_argument("--wait-secs", type=int, default=1800,
                   help="遇到 3002 后等待秒数（默认 1800=30min）")
    return p


def _new_server_client(cfg: Config):
    return new_http_client(
        cfg.server.base_url, cfg.server.api_key, cfg.server.timeout,
    )


def _notify(webhook: str, message: str, level: str) -> bool:
    return notify_wecom(webhook, message, level)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    logger = setup_logging(cfg.paths.log_dir, "signal_query")
    today = args.today or datetime.now().strftime("%Y%m%d")
    logger.info("开始查询信号，today=%s", today)

    # 初始化 xtquant 以便算下一交易日
    init_xtquant(cfg.qmt.data_dir)
    try:
        next_date = next_trading_day(today)
    except (ValueError, RuntimeError) as e:
        logger.error("next_trading_day 失败: %s", e)
        _notify(cfg.notify.wecom_webhook,
                f"信号查询 next_trading_day 失败: {e}", level="alert")
        return 2

    logger.info("next_trade_date=%s", next_date)

    with _new_server_client(cfg) as client:
        result = fetch_signals(next_date, client, wait_secs=args.wait_secs)

    if result.state == "NO_SIGNALS":
        _notify(cfg.notify.wecom_webhook, f"今日无交易信号（{next_date}）", "info")
        return 0

    if result.state == "STILL_PENDING":
        _notify(cfg.notify.wecom_webhook,
                f"信号查询失败 ({next_date})：服务器策略未完成（3002）重试后仍未就绪",
                "alert")
        return 3

    if result.state == "ERROR":
        _notify(cfg.notify.wecom_webhook,
                f"信号查询失败 ({next_date})：{result.error}", "alert")
        return 3

    # HAS_SIGNALS
    valid, rejected = validate_signals(result.signals, expected_date=next_date)
    if rejected:
        logger.warning("%d 条信号被拒：%s",
                       len(rejected),
                       [(r.get('signal_id'), r.get('_reason')) for r in rejected])

    if not valid:
        _notify(cfg.notify.wecom_webhook,
                f"信号查询拿到 {len(result.signals)} 条但全部不合法（{next_date}）",
                "alert")
        return 3

    db_path = Path(cfg.paths.sqlite_path)
    conn = get_connection(db_path)
    try:
        init_schema(conn)
        n = save_signals(conn, valid)
    finally:
        conn.close()

    summary_parts = [f"已制单（{next_date}），共 {n} 条信号"]
    if rejected:
        summary_parts.append(f"{len(rejected)} 条被拒")
    _notify(cfg.notify.wecom_webhook, "；".join(summary_parts), "info")
    logger.info("完成。valid=%d rejected=%d", n, len(rejected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/signal_query/test_cli.py -v
```

预期：4 个测试 PASS。

- [ ] **Step 5: 全回归**

```bash
pytest -v
```

预期：Plan A + B + C 所有测试全绿。

- [ ] **Step 6: Commit**

```bash
git add src/signal_query/__main__.py tests/signal_query/test_cli.py
git commit -m "feat(signal-query): add CLI for daily 19:00 signal fetch & persist"
```

---

## Task 7: Windows 集成冒烟测试文档

**Files:**
- Create: `/Users/mameican/Desktop/server/docs/manual_tests/module3_signal_query_smoke_test.md`

- [ ] **Step 1: 写文档**

内容：

```markdown
# 模块三 Windows 集成冒烟测试

**前置条件:**
1. 模块一、二已跑通，服务器能正常接 `POST /market-data`
2. 与搭档约定好：当日 `POST /market-data` 之后，服务器会生成次日信号
3. `config/settings.yaml` 已配置 server + notify + sqlite_path
4. QMT 客户端正常登录（只为 `next_trading_day` 查交易日历）

**执行步骤（Windows PowerShell）:**

```powershell
cd C:\parttime\qmt模拟盘pipeline\server
C:\parttime\qmt数据推送\venv\Scripts\activate
python -m src.signal_query --today 20260421 --config config\settings.yaml
```

**有信号场景验收:**

- [ ] 退出码 0
- [ ] 日志显示 `state=HAS_SIGNALS`，`signals 表 upsert N 行`
- [ ] 企业微信收到"已制单（20260422），共 N 条信号"
- [ ] SQLite 查询确认：
  ```python
  import sqlite3
  c = sqlite3.connect("data/trading.db")
  print(c.execute("SELECT signal_id, symbol, direction, valid_date FROM signals "
                  "WHERE valid_date='20260422'").fetchall())
  ```

**无信号场景验收:**

- 让搭档手动把当日信号队列清空后再运行
- [ ] 退出码 0，企业微信收到"今日无交易信号（20260422）"（非报警）

**3002 场景验收:**

- 与搭档协调：让服务器在策略运行中返回 3002
- 运行时加 `--wait-secs 10` 加速重试
- [ ] 日志显示等待 10s 后重试；若仍 3002 → 退出码 3，企业微信收到 `[报警]`

**过期信号拒绝验收:**

- 让服务器返回 `valid_date=错误日期` 的信号
- [ ] 日志显示 `校验拒绝 N 条信号`，该信号未写入 signals 表；通知消息含"X 条被拒"

**schema 幂等验证:**

- 重复运行命令
- [ ] 同一 signal_id 不重复插入（`SELECT COUNT(*)` 不增长），quantity 等字段被新值覆盖
```

- [ ] **Step 2: Commit**

```bash
git add docs/manual_tests/module3_signal_query_smoke_test.md
git commit -m "docs: add smoke test checklist for module 3 (signal query)"
```

---

## 收尾清单

- [ ] 所有 Task commit 完成
- [ ] `pytest -v` 全绿（覆盖 Plan A + B + C）
- [ ] Windows 集成冒烟测试四类场景通过
- [ ] 与搭档约定 /signals 接口的 `code=3002` 返回条件（用于故障演练）

---

## 后续计划

- 模块四（竞价下单）：`docs/superpowers/plans/2026-04-22-module4-auction-order-submission.md`
- 模块五+六（成交回报）：`docs/superpowers/plans/2026-04-22-module5-6-trade-result-reporting.md`
