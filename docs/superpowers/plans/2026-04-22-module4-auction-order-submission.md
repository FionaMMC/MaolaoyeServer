# 模块四：竞价下单 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 次日 09:10 由 Windows 任务计划程序自动触发，读取 SQLite `signals` 表中当日有效的信号，在 09:15 集合竞价开始前按 `limit_price × (1 + price_offset)` 提交限价单到 QMT 模拟盘，委托结果写入 `orders` 表；启动失败（QMT 连接异常等）立即发送微信报警。

**Architecture:** 新增 `src/auction_order/` 模块。`trader_connector.py` 封装 `XtQuantTrader` 连接与 startup check。`price_calc.py`（inline 也可，这里单拎一个纯函数便于测试）负责计算报价。`signal_reader.py` 从 SQLite 读取当日有效信号。`submitter.py` 调用 QMT `order_stock` 并解析返回。`store.py` 写 `orders` 表。`__main__.py` 编排。**不支持 MARKET 单**（模拟盘限制），遇到 `order_type=MARKET` 直接拒绝并告警。

**Tech Stack:** Python 3.11, xtquant.xttrader（仅 Windows）, sqlite3（stdlib）, httpx（用于 notify）, pytest.

**前置:** Plan A / B / C 已完成。本 Plan 复用：
- `src/common/config.py`、`src/common/logging_setup.py`、`src/common/notify.py`
- `src/common/db.py`（signals/orders 表）
- `src/market_data_download/connector.py` 的 `init_xtquant`（XtTrade 也依赖 data_dir）

**运行时序约束:** 脚本必须在 09:15 前完成下单。设计按 09:10 启动，留 5 分钟容错。

---

## 文件结构

**新建（模块四专属）：**
- `/Users/mameican/Desktop/server/src/auction_order/__init__.py`
- `/Users/mameican/Desktop/server/src/auction_order/trader_connector.py`
- `/Users/mameican/Desktop/server/src/auction_order/price_calc.py`
- `/Users/mameican/Desktop/server/src/auction_order/signal_reader.py`
- `/Users/mameican/Desktop/server/src/auction_order/submitter.py`
- `/Users/mameican/Desktop/server/src/auction_order/store.py`
- `/Users/mameican/Desktop/server/src/auction_order/__main__.py`
- `/Users/mameican/Desktop/server/tests/auction_order/__init__.py`
- `/Users/mameican/Desktop/server/tests/auction_order/test_trader_connector.py`
- `/Users/mameican/Desktop/server/tests/auction_order/test_price_calc.py`
- `/Users/mameican/Desktop/server/tests/auction_order/test_signal_reader.py`
- `/Users/mameican/Desktop/server/tests/auction_order/test_submitter.py`
- `/Users/mameican/Desktop/server/tests/auction_order/test_store.py`
- `/Users/mameican/Desktop/server/tests/auction_order/test_cli.py`

**新建（Windows 任务计划程序脚本 + 集成冒烟测试文档）：**
- `/Users/mameican/Desktop/server/scripts/daily_0910_auction.bat`
- `/Users/mameican/Desktop/server/docs/manual_tests/module4_auction_smoke_test.md`

---

## Task 1: 报价计算（`price_calc.py`）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/auction_order/__init__.py`
- Create: `/Users/mameican/Desktop/server/src/auction_order/price_calc.py`
- Create: `/Users/mameican/Desktop/server/tests/auction_order/__init__.py`
- Create: `/Users/mameican/Desktop/server/tests/auction_order/test_price_calc.py`

**规则:**
- `submit_price = round(limit_price × (1 + price_offset), 2)`（A 股最小 0.01）
- 买入：`price_offset > 0`（超价买入）；卖出：`price_offset < 0`
- 不做涨跌停裁剪（委托被拒的情形由 QMT 处理）

- [ ] **Step 1: 写两个空 `__init__.py`**

```python
# 包标记
```

写到：
- `/Users/mameican/Desktop/server/src/auction_order/__init__.py`
- `/Users/mameican/Desktop/server/tests/auction_order/__init__.py`

- [ ] **Step 2: 先写失败测试**

```python
"""price_calc 测试"""
from __future__ import annotations

import math

import pytest

from src.auction_order.price_calc import compute_submit_price


def test_buy_positive_offset():
    # 1540 × 1.005 = 1547.7 → 保留两位
    p = compute_submit_price(limit_price=1540.0, price_offset=0.005,
                              direction="BUY")
    assert math.isclose(p, 1547.70)


def test_sell_negative_offset():
    # 10.0 × (1 - 0.005) = 9.95
    p = compute_submit_price(limit_price=10.0, price_offset=-0.005,
                              direction="SELL")
    assert math.isclose(p, 9.95)


def test_rounds_to_two_decimals():
    # 1.234 × 1.001 = 1.235234 → 1.24
    p = compute_submit_price(limit_price=1.234, price_offset=0.001,
                              direction="BUY")
    assert p == 1.24


def test_zero_offset_equals_limit():
    p = compute_submit_price(limit_price=20.0, price_offset=0.0, direction="BUY")
    assert p == 20.00


def test_buy_with_negative_offset_raises():
    """买入应超价，offset 必须 >= 0；否则是方向错配，拒绝。"""
    with pytest.raises(ValueError, match="BUY"):
        compute_submit_price(limit_price=10.0, price_offset=-0.005,
                             direction="BUY")


def test_sell_with_positive_offset_raises():
    with pytest.raises(ValueError, match="SELL"):
        compute_submit_price(limit_price=10.0, price_offset=0.005,
                             direction="SELL")


def test_none_limit_price_raises():
    with pytest.raises(ValueError, match="limit_price"):
        compute_submit_price(limit_price=None, price_offset=0.005,
                             direction="BUY")
```

- [ ] **Step 3: 跑测试确认失败**

```bash
source /Users/mameican/Desktop/server/venv/bin/activate
pytest tests/auction_order/test_price_calc.py -v
```

预期：ImportError。

- [ ] **Step 4: 实现 `price_calc.py`**

```python
"""竞价报价计算：limit_price × (1 + price_offset)，两位小数。"""
from __future__ import annotations


def compute_submit_price(
    limit_price: float | None,
    price_offset: float,
    direction: str,
) -> float:
    """根据参考价和偏移计算实际报价。

    Raises:
        ValueError: 方向与偏移方向不匹配 / limit_price 为 None
    """
    if limit_price is None:
        raise ValueError("limit_price 不能为 None（MARKET 单由上游拒绝）")
    if direction == "BUY" and price_offset < 0:
        raise ValueError("BUY 方向要求 price_offset >= 0（超价买入），当前为负")
    if direction == "SELL" and price_offset > 0:
        raise ValueError("SELL 方向要求 price_offset <= 0（超价卖出为负），当前为正")

    raw = float(limit_price) * (1.0 + float(price_offset))
    return round(raw, 2)
```

- [ ] **Step 5: 跑测试确认通过**

```bash
pytest tests/auction_order/test_price_calc.py -v
```

预期：7 个测试 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/auction_order/__init__.py src/auction_order/price_calc.py tests/auction_order/__init__.py tests/auction_order/test_price_calc.py
git commit -m "feat(auction): add submit_price calculator with direction validation"
```

---

## Task 2: Signal Reader（从 SQLite 读当日有效信号）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/auction_order/signal_reader.py`
- Create: `/Users/mameican/Desktop/server/tests/auction_order/test_signal_reader.py`

**产出:** `read_active_signals(conn, today) -> list[Signal]`
- `today` 是当天日期（`YYYYMMDD`）
- 筛选 `valid_date == today` 的 `signals` 表行
- 排除已经在 `orders` 表中存在对应 `signal_id` 的记录（幂等：脚本重跑不重复下单）
- 返回 dataclass 列表，字段完整

- [ ] **Step 1: 先写失败测试**

```python
"""signal_reader 测试"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.db import get_connection, init_schema
from src.auction_order.signal_reader import Signal, read_active_signals


def _insert_signal(conn, sid, valid_date, **overrides):
    d = dict(
        signal_id=sid, symbol="600519.SH", direction="BUY", quantity=100,
        order_type="LIMIT", limit_price=1540.0, price_offset=0.005,
        strategy_id="s", signal_time="2026-04-21T18:30:00+08:00",
        valid_date=valid_date, fetched_at="2026-04-21T19:00:00+08:00",
    )
    d.update(overrides)
    conn.execute(
        """INSERT INTO signals
        (signal_id, symbol, direction, quantity, order_type, limit_price,
         price_offset, strategy_id, signal_time, valid_date, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (d["signal_id"], d["symbol"], d["direction"], d["quantity"],
         d["order_type"], d["limit_price"], d["price_offset"],
         d["strategy_id"], d["signal_time"], d["valid_date"], d["fetched_at"]),
    )
    conn.commit()


def _insert_order(conn, order_id, signal_id):
    conn.execute(
        """INSERT INTO orders
        (order_id, signal_id, symbol, direction, submitted_price,
         submitted_quantity, submitted_at, submit_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_id, signal_id, "600519.SH", "BUY",
         1547.7, 100, "2026-04-22T09:15:00+08:00", "SUCCESS"),
    )
    conn.commit()


def test_read_returns_signals_with_matching_valid_date(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _insert_signal(conn, "a", "20260422")
    _insert_signal(conn, "b", "20260422")
    _insert_signal(conn, "c", "20260423")  # 不同日期

    sigs = read_active_signals(conn, today="20260422")

    assert [s.signal_id for s in sigs] == ["a", "b"]
    assert isinstance(sigs[0], Signal)
    assert sigs[0].symbol == "600519.SH"
    assert sigs[0].quantity == 100


def test_read_skips_signals_already_in_orders(tmp_path: Path):
    """幂等：如果某 signal_id 已经下过单（在 orders 表里），本次跳过。"""
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _insert_signal(conn, "a", "20260422")
    _insert_signal(conn, "b", "20260422")
    _insert_order(conn, order_id="order-a", signal_id="a")

    sigs = read_active_signals(conn, today="20260422")

    assert [s.signal_id for s in sigs] == ["b"]


def test_read_empty_when_no_signals(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)

    sigs = read_active_signals(conn, today="20260422")
    assert sigs == []


def test_read_preserves_all_fields(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _insert_signal(conn, "a", "20260422",
                   direction="SELL", price_offset=-0.003,
                   limit_price=99.5, quantity=300)

    sigs = read_active_signals(conn, today="20260422")

    s = sigs[0]
    assert s.signal_id == "a"
    assert s.direction == "SELL"
    assert s.price_offset == -0.003
    assert s.limit_price == 99.5
    assert s.quantity == 300
    assert s.order_type == "LIMIT"


def test_read_null_limit_price_is_preserved(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _insert_signal(conn, "a", "20260422",
                   order_type="MARKET", limit_price=None)

    sigs = read_active_signals(conn, today="20260422")
    assert sigs[0].order_type == "MARKET"
    assert sigs[0].limit_price is None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/auction_order/test_signal_reader.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `signal_reader.py`**

```python
"""从 SQLite signals 表读取当日有效信号（排除已下单的）。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    signal_id: str
    symbol: str
    direction: str
    quantity: int
    order_type: str
    limit_price: float | None
    price_offset: float
    strategy_id: str
    signal_time: str
    valid_date: str


_SQL = """
SELECT s.signal_id, s.symbol, s.direction, s.quantity, s.order_type,
       s.limit_price, s.price_offset, s.strategy_id,
       s.signal_time, s.valid_date
FROM signals s
WHERE s.valid_date = ?
  AND NOT EXISTS (
    SELECT 1 FROM orders o WHERE o.signal_id = s.signal_id
  )
ORDER BY s.signal_id
"""


def read_active_signals(conn: sqlite3.Connection, today: str) -> list[Signal]:
    """返回 valid_date=today 且尚未下过单的信号。"""
    cur = conn.execute(_SQL, (today,))
    return [
        Signal(
            signal_id=r["signal_id"],
            symbol=r["symbol"],
            direction=r["direction"],
            quantity=int(r["quantity"]),
            order_type=r["order_type"],
            limit_price=(None if r["limit_price"] is None else float(r["limit_price"])),
            price_offset=float(r["price_offset"]),
            strategy_id=r["strategy_id"],
            signal_time=r["signal_time"],
            valid_date=r["valid_date"],
        )
        for r in cur.fetchall()
    ]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/auction_order/test_signal_reader.py -v
```

预期：5 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/auction_order/signal_reader.py tests/auction_order/test_signal_reader.py
git commit -m "feat(auction): add signal_reader filtering active signals not yet ordered"
```

---

## Task 3: Trader Connector（XtQuantTrader 连接与 startup check）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/auction_order/trader_connector.py`
- Create: `/Users/mameican/Desktop/server/tests/auction_order/test_trader_connector.py`

**产出:**
- `connect_trader(data_dir, account_id) -> (trader, account)` — 建连接、订阅账号，失败抛 `RuntimeError`

**QMT xttrader 调用顺序（参考官方文档）:**
1. `trader = XtQuantTrader(path, session_id)` — path 即 userdata_mini
2. `trader.start()`
3. `ret = trader.connect()` — `ret == 0` 成功
4. `account = StockAccount(account_id, "STOCK")`
5. `trader.subscribe(account)`

- [ ] **Step 1: 先写失败测试**

```python
"""trader_connector 测试：全程 mock xtquant.xttrader / xttype"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_xttrader(monkeypatch):
    """注入 xtquant.xttrader / xtquant.xttype / xtquant.xtconstant fakes"""
    trader_instance = MagicMock()
    trader_instance.start = MagicMock(return_value=None)
    trader_instance.connect = MagicMock(return_value=0)
    trader_instance.subscribe = MagicMock(return_value=0)

    XtQuantTraderCls = MagicMock(return_value=trader_instance)
    StockAccountCls = MagicMock(side_effect=lambda aid, t: SimpleNamespace(
        account_id=aid, account_type=t,
    ))

    fake_xttrader = SimpleNamespace(
        XtQuantTrader=XtQuantTraderCls,
        XtQuantTraderCallback=object,
    )
    fake_xttype = SimpleNamespace(StockAccount=StockAccountCls)
    fake_xtconstant = SimpleNamespace(
        STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11,
    )
    # xtdata 也需要 init_xtquant 用到
    fake_xtdata = SimpleNamespace(data_dir="")

    pkg = SimpleNamespace(
        xttrader=fake_xttrader, xttype=fake_xttype,
        xtconstant=fake_xtconstant, xtdata=fake_xtdata,
    )
    for n, m in [
        ("xtquant", pkg),
        ("xtquant.xttrader", fake_xttrader),
        ("xtquant.xttype", fake_xttype),
        ("xtquant.xtconstant", fake_xtconstant),
        ("xtquant.xtdata", fake_xtdata),
    ]:
        monkeypatch.setitem(sys.modules, n, m)

    return SimpleNamespace(
        trader_instance=trader_instance,
        XtQuantTraderCls=XtQuantTraderCls,
        StockAccountCls=StockAccountCls,
    )


def test_connect_trader_returns_trader_and_account(fake_xttrader):
    from src.auction_order.trader_connector import connect_trader

    trader, account = connect_trader(
        data_dir="/tmp/fake_qmt", account_id="ACC123",
    )

    assert trader is fake_xttrader.trader_instance
    assert account.account_id == "ACC123"
    assert account.account_type == "STOCK"
    fake_xttrader.trader_instance.start.assert_called_once()
    fake_xttrader.trader_instance.connect.assert_called_once()
    fake_xttrader.trader_instance.subscribe.assert_called_once_with(account)


def test_connect_trader_connect_fail_raises(fake_xttrader):
    from src.auction_order.trader_connector import connect_trader

    fake_xttrader.trader_instance.connect.return_value = -1

    with pytest.raises(RuntimeError, match="connect"):
        connect_trader(data_dir="/tmp/fake_qmt", account_id="ACC")


def test_connect_trader_subscribe_fail_raises(fake_xttrader):
    from src.auction_order.trader_connector import connect_trader

    fake_xttrader.trader_instance.subscribe.return_value = -1

    with pytest.raises(RuntimeError, match="subscribe"):
        connect_trader(data_dir="/tmp/fake_qmt", account_id="ACC")


def test_connect_trader_empty_account_raises(fake_xttrader):
    from src.auction_order.trader_connector import connect_trader

    with pytest.raises(ValueError, match="account_id"):
        connect_trader(data_dir="/tmp/fake_qmt", account_id="")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/auction_order/test_trader_connector.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `trader_connector.py`**

```python
"""XtQuantTrader 连接封装。失败立即抛异常，调用方负责发报警。"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def connect_trader(data_dir: str, account_id: str) -> tuple[Any, Any]:
    """建立 QMT 交易连接，返回 (trader, account)。

    Raises:
        ValueError: account_id 空
        RuntimeError: connect 或 subscribe 失败
    """
    if not account_id:
        raise ValueError("account_id 不能为空")

    from xtquant.xttrader import XtQuantTrader
    from xtquant.xttype import StockAccount

    session = int(time.time())
    trader = XtQuantTrader(data_dir, session)
    trader.start()

    ret = trader.connect()
    if ret != 0:
        raise RuntimeError(f"XtQuantTrader.connect() 返回 {ret}（非 0 表示连接失败）")
    logger.info("XtQuantTrader connected, session=%d", session)

    account = StockAccount(account_id, "STOCK")
    sub_ret = trader.subscribe(account)
    if sub_ret != 0:
        raise RuntimeError(f"XtQuantTrader.subscribe({account_id}) 返回 {sub_ret}")
    logger.info("XtQuantTrader subscribed account=%s", account_id)

    return trader, account
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/auction_order/test_trader_connector.py -v
```

预期：4 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/auction_order/trader_connector.py tests/auction_order/test_trader_connector.py
git commit -m "feat(auction): add XtQuantTrader connector with startup failure semantics"
```

---

## Task 4: Order Submitter

**Files:**
- Create: `/Users/mameican/Desktop/server/src/auction_order/submitter.py`
- Create: `/Users/mameican/Desktop/server/tests/auction_order/test_submitter.py`

**产出:** `submit_order(trader, account, signal) -> SubmitResult`

`SubmitResult`:
- `ok: bool`
- `order_id: str | None`
- `submitted_price: float | None`
- `submitted_quantity: int | None`
- `submitted_at: str | None` — ISO 8601
- `error: str | None`

**规则:**
- `order_type == "MARKET"` 直接拒绝（模拟盘不支持）
- 调用 `trader.order_stock(account, symbol, direction_code, quantity, FIX_PRICE, price, strategy, remark)`
- `direction` → `xtconstant.STOCK_BUY` / `STOCK_SELL`
- 返回的 `order_id` < 0 视为失败

- [ ] **Step 1: 先写失败测试**

```python
"""submitter 测试：mock trader + 共享 fake_xttrader fixture 的 xtconstant"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.auction_order.signal_reader import Signal


@pytest.fixture
def fake_xtconstant(monkeypatch):
    const = SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11)
    monkeypatch.setitem(sys.modules, "xtquant.xtconstant", const)
    monkeypatch.setitem(sys.modules, "xtquant",
                        SimpleNamespace(xtconstant=const))
    return const


def _sig(**overrides) -> Signal:
    kw = dict(
        signal_id="sig1", symbol="600519.SH", direction="BUY",
        quantity=100, order_type="LIMIT",
        limit_price=1540.0, price_offset=0.005, strategy_id="s",
        signal_time="2026-04-21T18:30:00+08:00", valid_date="20260422",
    )
    kw.update(overrides)
    return Signal(**kw)


def test_submit_happy_path_buy(fake_xtconstant):
    from src.auction_order.submitter import submit_order

    trader = MagicMock()
    trader.order_stock = MagicMock(return_value=123456)  # 正数 = 成功
    account = SimpleNamespace(account_id="ACC")

    r = submit_order(trader, account, _sig())

    assert r.ok is True
    assert r.order_id == "123456"
    assert r.submitted_price == 1547.70
    assert r.submitted_quantity == 100
    assert r.submitted_at is not None
    assert r.error is None

    args, kwargs = trader.order_stock.call_args
    assert args[0] is account or kwargs.get("account") is account
    # 校验 price_type 是 FIX_PRICE
    call_args_flat = list(args) + list(kwargs.values())
    assert fake_xtconstant.FIX_PRICE in call_args_flat
    assert fake_xtconstant.STOCK_BUY in call_args_flat


def test_submit_happy_path_sell(fake_xtconstant):
    from src.auction_order.submitter import submit_order

    trader = MagicMock()
    trader.order_stock = MagicMock(return_value=789)
    account = SimpleNamespace(account_id="ACC")

    r = submit_order(trader, account,
                     _sig(direction="SELL", price_offset=-0.005))

    assert r.ok is True
    assert r.submitted_price == 10.0 * (1 - 0.005) * 0 + round(1540.0 * (1 - 0.005), 2)
    call_args_flat = list(trader.order_stock.call_args.args) + \
                     list(trader.order_stock.call_args.kwargs.values())
    assert fake_xtconstant.STOCK_SELL in call_args_flat


def test_submit_rejects_market_order_type(fake_xtconstant):
    from src.auction_order.submitter import submit_order

    trader = MagicMock()
    account = SimpleNamespace(account_id="ACC")

    r = submit_order(trader, account,
                     _sig(order_type="MARKET", limit_price=None))

    assert r.ok is False
    assert "MARKET" in (r.error or "")
    trader.order_stock.assert_not_called()


def test_submit_negative_order_id_is_failure(fake_xtconstant):
    from src.auction_order.submitter import submit_order

    trader = MagicMock()
    trader.order_stock = MagicMock(return_value=-1)
    account = SimpleNamespace(account_id="ACC")

    r = submit_order(trader, account, _sig())

    assert r.ok is False
    assert r.order_id is None
    assert "-1" in (r.error or "")


def test_submit_exception_captured(fake_xtconstant):
    from src.auction_order.submitter import submit_order

    trader = MagicMock()
    trader.order_stock = MagicMock(side_effect=RuntimeError("boom"))
    account = SimpleNamespace(account_id="ACC")

    r = submit_order(trader, account, _sig())

    assert r.ok is False
    assert "boom" in (r.error or "")


def test_submit_price_calculation_error(fake_xtconstant):
    from src.auction_order.submitter import submit_order

    trader = MagicMock()
    account = SimpleNamespace(account_id="ACC")

    # direction BUY + 负 offset → 报价计算抛异常 → 被捕获
    r = submit_order(trader, account,
                     _sig(direction="BUY", price_offset=-0.005))

    assert r.ok is False
    assert "price_offset" in (r.error or "") or "BUY" in (r.error or "")
    trader.order_stock.assert_not_called()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/auction_order/test_submitter.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `submitter.py`**

```python
"""调用 QMT order_stock 提交限价单。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.auction_order.price_calc import compute_submit_price
from src.auction_order.signal_reader import Signal

logger = logging.getLogger(__name__)


@dataclass
class SubmitResult:
    ok: bool
    order_id: str | None = None
    submitted_price: float | None = None
    submitted_quantity: int | None = None
    submitted_at: str | None = None
    error: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def submit_order(trader: Any, account: Any, signal: Signal) -> SubmitResult:
    """提交单条限价委托。不抛异常，失败原因放进 SubmitResult.error。"""
    if signal.order_type == "MARKET":
        return SubmitResult(
            ok=False,
            error="MARKET 单被拒（模拟盘不支持市价单）",
        )

    try:
        price = compute_submit_price(
            signal.limit_price, signal.price_offset, signal.direction,
        )
    except ValueError as e:
        return SubmitResult(ok=False, error=f"price_calc 失败: {e}")

    from xtquant import xtconstant

    direction_code = (
        xtconstant.STOCK_BUY if signal.direction == "BUY"
        else xtconstant.STOCK_SELL
    )

    try:
        order_id = trader.order_stock(
            account=account,
            stock_code=signal.symbol,
            order_type=direction_code,
            order_volume=int(signal.quantity),
            price_type=xtconstant.FIX_PRICE,
            price=price,
            strategy_name=signal.strategy_id,
            order_remark=signal.signal_id,
        )
    except Exception as e:  # noqa: BLE001
        return SubmitResult(ok=False, error=f"order_stock 异常: {e}")

    if not isinstance(order_id, int) or order_id < 0:
        return SubmitResult(ok=False, error=f"order_stock 返回 {order_id}")

    logger.info("signal_id=%s 委托成功 order_id=%s price=%s qty=%s",
                signal.signal_id, order_id, price, signal.quantity)
    return SubmitResult(
        ok=True,
        order_id=str(order_id),
        submitted_price=price,
        submitted_quantity=int(signal.quantity),
        submitted_at=_now_iso(),
    )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/auction_order/test_submitter.py -v
```

预期：6 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/auction_order/submitter.py tests/auction_order/test_submitter.py
git commit -m "feat(auction): add order submitter (FIX_PRICE only, MARKET rejected)"
```

---

## Task 5: Orders Store

**Files:**
- Create: `/Users/mameican/Desktop/server/src/auction_order/store.py`
- Create: `/Users/mameican/Desktop/server/tests/auction_order/test_store.py`

**产出:** `save_orders(conn, records) -> int`，`records` 是：

```python
@dataclass
class OrderRecord:
    order_id: str
    signal_id: str
    symbol: str
    direction: str
    submitted_price: float
    submitted_quantity: int
    submitted_at: str
    submit_status: str  # SUCCESS / FAILED
```

**注意:** `submit_status=FAILED` 的记录 `order_id` 用 `"fail-{signal_id}"` 占位（orders 表 PK 不能为空），`submitted_price`/`submitted_quantity` 可填 0。

- [ ] **Step 1: 先写失败测试**

```python
"""auction_order.store 测试"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.db import get_connection, init_schema
from src.auction_order.store import OrderRecord, save_orders


def _signal_row(conn, sid: str = "sig1"):
    conn.execute(
        """INSERT INTO signals
        (signal_id, symbol, direction, quantity, order_type, limit_price,
         price_offset, strategy_id, signal_time, valid_date, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, "600519.SH", "BUY", 100, "LIMIT", 1540.0, 0.005,
         "s", "2026-04-21T18:30:00+08:00", "20260422",
         "2026-04-21T19:00:00+08:00"),
    )
    conn.commit()


def test_save_orders_success(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _signal_row(conn, "sig1")

    recs = [OrderRecord(
        order_id="123456", signal_id="sig1", symbol="600519.SH",
        direction="BUY", submitted_price=1547.7, submitted_quantity=100,
        submitted_at="2026-04-22T09:15:00+08:00", submit_status="SUCCESS",
    )]

    n = save_orders(conn, recs)
    assert n == 1

    row = conn.execute(
        "SELECT order_id, signal_id, submit_status FROM orders"
    ).fetchone()
    assert row[0] == "123456"
    assert row[1] == "sig1"
    assert row[2] == "SUCCESS"


def test_save_orders_failed_uses_placeholder_id(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _signal_row(conn, "sig1")

    recs = [OrderRecord(
        order_id="fail-sig1", signal_id="sig1", symbol="600519.SH",
        direction="BUY", submitted_price=0.0, submitted_quantity=0,
        submitted_at="2026-04-22T09:15:00+08:00", submit_status="FAILED",
    )]

    save_orders(conn, recs)

    row = conn.execute(
        "SELECT order_id, submit_status FROM orders WHERE signal_id='sig1'"
    ).fetchone()
    assert row[0] == "fail-sig1"
    assert row[1] == "FAILED"


def test_save_orders_empty_list(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)

    assert save_orders(conn, []) == 0


def test_save_orders_multiple(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _signal_row(conn, "a")
    _signal_row(conn, "b")

    recs = [
        OrderRecord("o1", "a", "600519.SH", "BUY", 100.0, 100,
                    "2026-04-22T09:15:00+08:00", "SUCCESS"),
        OrderRecord("fail-b", "b", "000001.SZ", "BUY", 0, 0,
                    "2026-04-22T09:15:01+08:00", "FAILED"),
    ]

    n = save_orders(conn, recs)
    assert n == 2
    cnt = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert cnt == 2
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/auction_order/test_store.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `store.py`**

```python
"""写入 orders 表。"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    signal_id: str
    symbol: str
    direction: str
    submitted_price: float
    submitted_quantity: int
    submitted_at: str
    submit_status: str  # SUCCESS / FAILED


_INSERT_SQL = """
INSERT OR REPLACE INTO orders
(order_id, signal_id, symbol, direction,
 submitted_price, submitted_quantity, submitted_at, submit_status)
VALUES (?, ?, ?, ?, ?, ?, ?, ?);
"""


def save_orders(conn: sqlite3.Connection, records: list[OrderRecord]) -> int:
    if not records:
        return 0

    rows = [
        (r.order_id, r.signal_id, r.symbol, r.direction,
         float(r.submitted_price), int(r.submitted_quantity),
         r.submitted_at, r.submit_status)
        for r in records
    ]
    conn.executemany(_INSERT_SQL, rows)
    conn.commit()
    logger.info("orders 表写入 %d 行", len(rows))
    return len(rows)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/auction_order/test_store.py -v
```

预期：4 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/auction_order/store.py tests/auction_order/test_store.py
git commit -m "feat(auction): add orders table writer with FAILED placeholder id"
```

---

## Task 6: CLI 编排

**Files:**
- Create: `/Users/mameican/Desktop/server/src/auction_order/__main__.py`
- Create: `/Users/mameican/Desktop/server/tests/auction_order/test_cli.py`

**用法:**

```bash
python -m src.auction_order --today 20260422 --config config/settings.yaml
```

`--today` 缺省取系统当天。

**退出码:**
- `0` 所有信号成功下单（或本来就没信号）
- `1` 配置/参数错误
- `2` QMT 连接失败（startup_check / connect_trader）— 已发 alert
- `3` 部分委托失败（已发 alert，含失败详情）

**通知:**
- 启动失败：`[报警] 下单脚本启动失败：QMT 连接异常 ...`
- 无信号：不发（此脚本只在 09:10 跑，正常应有信号；无信号由模块三的 19:00 脚本负责提示）
- 全部成功：`竞价下单完成：N 单成功` info
- 部分失败：`[报警] 竞价下单 N 单成功 / M 单失败，详情：...`

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
  account_id: "ACC123"
server:
  base_url: "https://srv"
  api_key: "KEY"
  timeout: 10
paths:
  data_root: "{data_root}"
  log_dir: "{data_root}/logs"
  sqlite_path: "{data_root}/trading.db"
notify:
  wecom_webhook: "https://wecom"
market_data:
  sector_name: "沪深A股"
""", encoding="utf-8")
    return p


def _write_signal(db_path: Path, sid="s1", valid="20260422", **overrides):
    from src.common.db import get_connection, init_schema
    conn = get_connection(db_path)
    init_schema(conn)
    d = dict(
        signal_id=sid, symbol="600519.SH", direction="BUY", quantity=100,
        order_type="LIMIT", limit_price=1540.0, price_offset=0.005,
        strategy_id="s", signal_time="2026-04-21T18:30:00+08:00",
        valid_date=valid, fetched_at="2026-04-21T19:00:00+08:00",
    )
    d.update(overrides)
    conn.execute(
        """INSERT INTO signals
        (signal_id, symbol, direction, quantity, order_type, limit_price,
         price_offset, strategy_id, signal_time, valid_date, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (d["signal_id"], d["symbol"], d["direction"], d["quantity"],
         d["order_type"], d["limit_price"], d["price_offset"],
         d["strategy_id"], d["signal_time"], d["valid_date"], d["fetched_at"]),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def fake_xt(monkeypatch):
    """注入 xtquant 子模块的 mocks（xttrader、xttype、xtconstant、xtdata）"""
    trader = MagicMock()
    trader.start = MagicMock(return_value=None)
    trader.connect = MagicMock(return_value=0)
    trader.subscribe = MagicMock(return_value=0)
    trader.order_stock = MagicMock(return_value=111)

    XtQuantTraderCls = MagicMock(return_value=trader)
    StockAccountCls = MagicMock(side_effect=lambda aid, t: SimpleNamespace(
        account_id=aid, account_type=t,
    ))

    fake_xttrader = SimpleNamespace(
        XtQuantTrader=XtQuantTraderCls,
        XtQuantTraderCallback=object,
    )
    fake_xttype = SimpleNamespace(StockAccount=StockAccountCls)
    fake_xtconstant = SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11)
    fake_xtdata = SimpleNamespace(data_dir="")

    pkg = SimpleNamespace(
        xttrader=fake_xttrader, xttype=fake_xttype,
        xtconstant=fake_xtconstant, xtdata=fake_xtdata,
    )
    for n, m in [
        ("xtquant", pkg),
        ("xtquant.xttrader", fake_xttrader),
        ("xtquant.xttype", fake_xttype),
        ("xtquant.xtconstant", fake_xtconstant),
        ("xtquant.xtdata", fake_xtdata),
    ]:
        monkeypatch.setitem(sys.modules, n, m)

    return SimpleNamespace(trader=trader)


def test_cli_happy_path(fake_xt, tmp_path: Path, monkeypatch):
    from src.auction_order import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _write_signal(data_root / "trading.db", sid="s1", valid="20260422")
    _write_signal(data_root / "trading.db", sid="s2", valid="20260422")

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))

    exit_code = cli_mod.main(["--today", "20260422", "--config", str(cfg)])

    assert exit_code == 0
    assert fake_xt.trader.order_stock.call_count == 2
    assert any("下单完成" in c["text"]["content"] or "成功" in c["text"]["content"]
               for c in wecom_calls)


def test_cli_trader_connect_fail_alerts(fake_xt, tmp_path: Path, monkeypatch):
    from src.auction_order import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _write_signal(data_root / "trading.db", sid="s1", valid="20260422")

    fake_xt.trader.connect.return_value = -1

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))

    exit_code = cli_mod.main(["--today", "20260422", "--config", str(cfg)])

    assert exit_code == 2
    assert any(c["text"]["content"].startswith("[报警]") for c in wecom_calls)


def test_cli_market_signal_rejected_but_continues(fake_xt, tmp_path: Path, monkeypatch):
    from src.auction_order import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _write_signal(data_root / "trading.db", sid="good", valid="20260422")
    _write_signal(data_root / "trading.db", sid="bad", valid="20260422",
                  order_type="MARKET", limit_price=None)

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))

    exit_code = cli_mod.main(["--today", "20260422", "--config", str(cfg)])

    assert exit_code == 3  # 有失败 → alert
    assert fake_xt.trader.order_stock.call_count == 1  # 只下了 good
    assert any(c["text"]["content"].startswith("[报警]") for c in wecom_calls)


def test_cli_no_active_signals_ok(fake_xt, tmp_path: Path, monkeypatch):
    """signals 表为空或 valid_date 不匹配，退出 0，无报警。"""
    from src.auction_order import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    # 只有历史日期的信号
    _write_signal(data_root / "trading.db", sid="old", valid="20260421")

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))

    exit_code = cli_mod.main(["--today", "20260422", "--config", str(cfg)])

    assert exit_code == 0
    fake_xt.trader.order_stock.assert_not_called()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/auction_order/test_cli.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `__main__.py`**

```python
"""CLI：python -m src.auction_order --today YYYYMMDD --config path

09:10 Windows 任务计划程序触发。必须在 09:15 前完成下单。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from src.auction_order.signal_reader import read_active_signals
from src.auction_order.store import OrderRecord, save_orders
from src.auction_order.submitter import submit_order
from src.auction_order.trader_connector import connect_trader
from src.common.config import load_config
from src.common.db import get_connection, init_schema
from src.common.logging_setup import setup_logging
from src.common.notify import notify_wecom
from src.market_data_download.connector import init_xtquant


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.auction_order",
        description="09:10 触发：读 signals 表、提交限价单、写 orders 表。",
    )
    p.add_argument("--today", help="YYYYMMDD；缺省取本机当天")
    p.add_argument("--config", required=True, help="settings.yaml 路径")
    return p


def _notify(webhook: str, message: str, level: str) -> bool:
    return notify_wecom(webhook, message, level)


def _fail_record(sig, error: str) -> OrderRecord:
    return OrderRecord(
        order_id=f"fail-{sig.signal_id}",
        signal_id=sig.signal_id,
        symbol=sig.symbol,
        direction=sig.direction,
        submitted_price=0.0,
        submitted_quantity=0,
        submitted_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        submit_status="FAILED",
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    logger = setup_logging(cfg.paths.log_dir, "auction_order")
    today = args.today or datetime.now().strftime("%Y%m%d")
    logger.info("开始竞价下单，today=%s", today)

    # 初始化 xtdata（xttrader 也依赖 data_dir）
    init_xtquant(cfg.qmt.data_dir)

    try:
        trader, account = connect_trader(
            data_dir=cfg.qmt.data_dir,
            account_id=cfg.qmt.account_id,
        )
    except (ValueError, RuntimeError) as e:
        logger.error("QMT 连接失败: %s", e)
        _notify(cfg.notify.wecom_webhook,
                f"下单脚本启动失败：QMT 连接异常 — {e}", "alert")
        return 2

    db_path = Path(cfg.paths.sqlite_path)
    conn = get_connection(db_path)
    try:
        init_schema(conn)
        signals = read_active_signals(conn, today=today)
    finally:
        # 读完关，下单后再开（避免长持锁）
        pass

    if not signals:
        logger.info("当日无待下单信号")
        # 无信号不通知（由 19:00 脚本负责提示）
        conn.close()
        return 0

    logger.info("待下单 %d 条", len(signals))
    records: list[OrderRecord] = []
    success = 0
    failures: list[tuple[str, str]] = []  # (signal_id, error)

    for sig in signals:
        r = submit_order(trader, account, sig)
        if r.ok:
            records.append(OrderRecord(
                order_id=r.order_id or "",
                signal_id=sig.signal_id,
                symbol=sig.symbol,
                direction=sig.direction,
                submitted_price=r.submitted_price or 0.0,
                submitted_quantity=r.submitted_quantity or 0,
                submitted_at=r.submitted_at or "",
                submit_status="SUCCESS",
            ))
            success += 1
        else:
            records.append(_fail_record(sig, r.error or "unknown"))
            failures.append((sig.signal_id, r.error or "unknown"))
            logger.error("signal_id=%s 下单失败: %s", sig.signal_id, r.error)

    save_orders(conn, records)
    conn.close()

    if not failures:
        _notify(cfg.notify.wecom_webhook,
                f"竞价下单完成：{success} 单成功（{today}）", "info")
        return 0

    detail_lines = [f"{sid}: {err}" for sid, err in failures[:10]]
    more = "" if len(failures) <= 10 else f"\n...还有 {len(failures) - 10} 条"
    _notify(cfg.notify.wecom_webhook,
            f"竞价下单完成（{today}）：{success} 单成功 / {len(failures)} 单失败\n"
            + "\n".join(detail_lines) + more,
            "alert")
    return 3


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/auction_order/test_cli.py -v
```

预期：4 个测试 PASS。

- [ ] **Step 5: 全回归**

```bash
pytest -v
```

预期：Plan A + B + C + D 所有测试全绿。

- [ ] **Step 6: Commit**

```bash
git add src/auction_order/__main__.py tests/auction_order/test_cli.py
git commit -m "feat(auction): add CLI orchestrating read-signals→submit→save-orders"
```

---

## Task 7: Windows 任务计划程序脚本

**Files:**
- Create: `/Users/mameican/Desktop/server/scripts/daily_0910_auction.bat`

**说明:** 方便 Windows 任务计划程序每个交易日 09:10 自动执行。脚本本身做的事：
1. 激活 venv
2. 切换到项目根目录
3. 运行 `python -m src.auction_order`
4. 日志重定向到带日期的文件

- [ ] **Step 1: 写批处理脚本**

```bat
@echo off
REM QMT 模拟盘 — 09:10 竞价下单触发脚本
REM Windows 任务计划程序配置：每交易日 09:10 执行一次

set PROJECT_ROOT=C:\parttime\qmt模拟盘pipeline\server
set VENV=C:\parttime\qmt数据推送\venv
set LOGDIR=%PROJECT_ROOT%\logs
set TODAY=%date:~0,4%%date:~5,2%%date:~8,2%

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

call "%VENV%\Scripts\activate.bat"
cd /d "%PROJECT_ROOT%"
python -m src.auction_order --today %TODAY% --config config\settings.yaml >> "%LOGDIR%\auction_%TODAY%.log" 2>&1
exit /b %ERRORLEVEL%
```

- [ ] **Step 2: Commit**

```bash
mkdir -p /Users/mameican/Desktop/server/scripts
git add scripts/daily_0910_auction.bat
git commit -m "chore(auction): add Windows batch script for 09:10 task scheduler"
```

---

## Task 8: Windows 集成冒烟测试文档

**Files:**
- Create: `/Users/mameican/Desktop/server/docs/manual_tests/module4_auction_smoke_test.md`

- [ ] **Step 1: 写文档**

内容：

```markdown
# 模块四 Windows 集成冒烟测试

**前置条件:**
1. Plan A/B/C 已上 Windows 并跑通；`data/trading.db` 的 `signals` 表里有明日 `valid_date` 的待下单信号
2. QMT 客户端已登录模拟盘账号（`settings.yaml` 的 `qmt.account_id` 与客户端一致）
3. QMT 客户端已启用交易功能（xttrader 接口需要在客户端设置里打开）
4. 本次是 **模拟盘**，不要用真金账号做冒烟测试

**干跑验证（任意时间均可）:**

```powershell
cd C:\parttime\qmt模拟盘pipeline\server
C:\parttime\qmt数据推送\venv\Scripts\activate
python -m src.auction_order --today 20260422 --config config\settings.yaml
```

- [ ] 退出码 0
- [ ] QMT 委托窗口看到对应 N 条限价委托，`price = limit_price × (1 + price_offset)`（精度 0.01）
- [ ] `orders` 表新增 N 行：
  ```python
  import sqlite3
  c = sqlite3.connect("data/trading.db")
  print(c.execute(
      "SELECT order_id, signal_id, submitted_price, submit_status "
      "FROM orders ORDER BY submitted_at DESC LIMIT 10"
  ).fetchall())
  ```
- [ ] 企业微信收到"竞价下单完成：N 单成功"

**幂等验证:**

- 立即重跑相同命令
- [ ] 第二次不再重复下单（`read_active_signals` 排除已存在的 signal_id）
- [ ] `orders` 表行数不增

**QMT 断开演练:**

- 关掉 QMT 客户端后跑命令
- [ ] 退出码 2
- [ ] 企业微信收到 `[报警] 下单脚本启动失败：QMT 连接异常 ...`

**MARKET 信号拒绝演练:**

- 手动 SQL 插入一条 `order_type='MARKET'` 的信号（仅测试）:
  ```sql
  INSERT INTO signals VALUES ('test-mkt', '600519.SH', 'BUY', 100,
    'MARKET', NULL, 0.005, 'test', '2026-04-21T18:30:00+08:00',
    '20260422', '2026-04-21T19:00:00+08:00');
  ```
- 跑命令
- [ ] 该信号的 orders 行 `submit_status='FAILED'`, `order_id='fail-test-mkt'`
- [ ] 企业微信收到 `[报警] 竞价下单完成 ... / 1 单失败`，其中包含 `test-mkt: MARKET 单被拒`
- 测试完成后记得 `DELETE FROM signals WHERE signal_id='test-mkt'` 并 `DELETE FROM orders WHERE signal_id='test-mkt'`

**真实触发验证（交易日 09:10）:**

1. 配置 Windows 任务计划程序：
   - 触发器：每交易日 09:10（或用日重复 + 排除周末）
   - 操作：启动程序 `C:\parttime\qmt模拟盘pipeline\server\scripts\daily_0910_auction.bat`
   - 选项：勾选"不管用户是否登录都运行"需要密码，若不勾则要保证该 Windows 账号已登录
2. 确认任务计划程序执行后：
   - [ ] `logs/auction_YYYYMMDD.log` 末尾显示完成
   - [ ] QMT 委托窗口 09:15 前已提交所有限价单
   - [ ] 09:25 集合竞价结束后查 QMT 是否成交（部分成交 / 全部成交 / 未成交）
   - 注：成交结果由模块五 09:35 查询并推服务器
```

- [ ] **Step 2: Commit**

```bash
git add docs/manual_tests/module4_auction_smoke_test.md
git commit -m "docs: add smoke test checklist for module 4 (auction order)"
```

---

## 收尾清单

- [ ] 所有 Task commit 完成
- [ ] `pytest -v` 全绿（Plan A + B + C + D）
- [ ] Windows 干跑验证通过
- [ ] Windows 任务计划程序已配置 09:10 每交易日触发
- [ ] 真实交易日首次实跑后保留 QMT 委托截图 + orders 表快照归档

---

## 风险记录

- **xttrader 连接竞态**：实盘经验里 `trader.connect()` 偶尔返回非 0 再重连才成功。当前实现是一次失败即退出 2。若后期出现稳定性问题，可在 `connect_trader` 内加最多 3 次重试（延迟 2s）。
- **session id**：当前用 `int(time.time())` 作为 session_id，跨进程并发会冲突。本项目只在 09:10 单进程运行，无并发；若未来扩展需改为 UUID 或进程锁。
- **order_stock 是异步**：QMT 返回 order_id 后实际成交还要等市场处理。本模块只记录"提交成功"，成交状态由模块五查询。

---

## 后续计划

- 模块五+六（成交回报）：`docs/superpowers/plans/2026-04-22-module5-6-trade-result-reporting.md`
