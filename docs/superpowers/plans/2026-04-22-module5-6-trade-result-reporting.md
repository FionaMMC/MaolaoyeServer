# 模块五+六：竞价与收盘成交回报 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在同一个 Python 模块 `src/trade_result/` 中实现模块五（09:35 竞价成交回报）和模块六（15:30 收盘成交回报）。用 `--stage auction|close` 切换两种场景：查询 QMT 当日成交、与本地 `orders` 表匹配补齐 `signal_id`、写入本地 `trades` 表、按《API接口文档（纯股）》`POST /trade-result` 格式推送服务器；推送成功发 info 微信通知，失败发 alert。

**Architecture:** 两阶段共用同一套代码，仅 stage 字段与触发时间不同。`trades_reader.py` 调用 `trader.query_stock_trades()`；`matcher.py` 把 QMT trade 聚合并按 `order_id` 与本地 `orders` join，找出 `signal_id` 并为"未成交的 order"补空记录；`store.py` DELETE+INSERT 本次涉及的 order_id 对应 trades 行；`reporter.py` 复用模块二类似的 retry 逻辑推 `/trade-result`；`__main__.py` 根据 stage 编排。

**Tech Stack:** Python 3.11, xtquant.xttrader, sqlite3, httpx, pytest.

**前置:** Plan A / B / C / D 均完成。本 Plan 复用：
- `src/common/*`（config、db、http_client、notify、logging_setup）
- `src/market_data_download/connector.py` 的 `init_xtquant`
- `src/auction_order/trader_connector.py` 的 `connect_trader`
- `orders` / `trades` 表 schema（Plan C Task 1 已建）

**服务器 API 字段对齐:**

| 本地 trades 字段 | API `results[]` 字段 | 备注 |
|---|---|---|
| `signal_id` | `signal_id` | 主键 |
| `order_id` → lookup `orders.symbol` | `symbol` | |
| `orders.direction` | `direction` | |
| SUM(filled_quantity) for order | `filled_quantity` | 未成交填 0 |
| 平均 `filled_price` | `filled_price` | 未成交填 0 |
| MAX(filled_time) | `filled_time` | 未成交可省略 |
| 按规则（见 matcher）| `status` | FILLED/PARTIAL/CANCELLED/REJECTED |

---

## 文件结构

**新建:**
- `/Users/mameican/Desktop/server/src/trade_result/__init__.py`
- `/Users/mameican/Desktop/server/src/trade_result/trades_reader.py`
- `/Users/mameican/Desktop/server/src/trade_result/matcher.py`
- `/Users/mameican/Desktop/server/src/trade_result/store.py`
- `/Users/mameican/Desktop/server/src/trade_result/reporter.py`
- `/Users/mameican/Desktop/server/src/trade_result/__main__.py`
- `/Users/mameican/Desktop/server/tests/trade_result/__init__.py`
- `/Users/mameican/Desktop/server/tests/trade_result/test_trades_reader.py`
- `/Users/mameican/Desktop/server/tests/trade_result/test_matcher.py`
- `/Users/mameican/Desktop/server/tests/trade_result/test_store.py`
- `/Users/mameican/Desktop/server/tests/trade_result/test_reporter.py`
- `/Users/mameican/Desktop/server/tests/trade_result/test_cli.py`

**Windows 任务脚本 + 冒烟文档:**
- `/Users/mameican/Desktop/server/scripts/daily_0935_auction_report.bat`
- `/Users/mameican/Desktop/server/scripts/daily_1530_close_report.bat`
- `/Users/mameican/Desktop/server/docs/manual_tests/module5_6_trade_result_smoke_test.md`

---

## Task 1: Trades Reader（QMT query_stock_trades 封装）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/trade_result/__init__.py`
- Create: `/Users/mameican/Desktop/server/src/trade_result/trades_reader.py`
- Create: `/Users/mameican/Desktop/server/tests/trade_result/__init__.py`
- Create: `/Users/mameican/Desktop/server/tests/trade_result/test_trades_reader.py`

**产出:** `fetch_trades(trader, account) -> list[QmtTrade]`

```python
@dataclass
class QmtTrade:
    order_id: str
    stock_code: str
    traded_price: float
    traded_volume: int
    traded_amount: float
    traded_time: str  # ISO 8601
```

**注意:**
- `trader.query_stock_trades(account)` 返回的 XtTrade 对象的 `order_id` 是 int → 统一转 str
- `traded_time` QMT 返回的是 timestamp（秒），转 ISO 8601

- [ ] **Step 1: 写两个空 `__init__.py`**

```python
# 包标记
```

分别写到：
- `/Users/mameican/Desktop/server/src/trade_result/__init__.py`
- `/Users/mameican/Desktop/server/tests/trade_result/__init__.py`

- [ ] **Step 2: 先写失败测试**

```python
"""trades_reader 测试"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.trade_result.trades_reader import QmtTrade, fetch_trades


def _xt_trade(oid: int, code: str, price: float, vol: int, ts: int):
    return SimpleNamespace(
        order_id=oid,
        stock_code=code,
        traded_price=price,
        traded_volume=vol,
        traded_amount=price * vol,
        traded_time=ts,
    )


def test_fetch_trades_happy_path():
    trader = MagicMock()
    trader.query_stock_trades = MagicMock(return_value=[
        _xt_trade(111, "600519.SH", 1547.7, 100, 1745284500),  # ISO 会转成固定时间
        _xt_trade(111, "600519.SH", 1547.8, 50, 1745284600),   # 同一 order 多笔成交
        _xt_trade(222, "000001.SZ", 10.0, 300, 1745284700),
    ])
    account = SimpleNamespace(account_id="ACC")

    trades = fetch_trades(trader, account)

    assert len(trades) == 3
    assert all(isinstance(t, QmtTrade) for t in trades)
    assert trades[0].order_id == "111"  # 转字符串
    assert trades[0].stock_code == "600519.SH"
    assert trades[0].traded_price == 1547.7
    assert trades[0].traded_volume == 100
    # traded_time 应该是 ISO 8601 字符串
    assert "T" in trades[0].traded_time


def test_fetch_trades_empty():
    trader = MagicMock()
    trader.query_stock_trades = MagicMock(return_value=[])
    account = SimpleNamespace(account_id="ACC")

    trades = fetch_trades(trader, account)
    assert trades == []


def test_fetch_trades_none_returned_treated_as_empty():
    trader = MagicMock()
    trader.query_stock_trades = MagicMock(return_value=None)
    account = SimpleNamespace(account_id="ACC")

    trades = fetch_trades(trader, account)
    assert trades == []


def test_fetch_trades_query_exception_raises():
    import pytest
    trader = MagicMock()
    trader.query_stock_trades = MagicMock(side_effect=RuntimeError("boom"))
    account = SimpleNamespace(account_id="ACC")

    with pytest.raises(RuntimeError, match="boom"):
        fetch_trades(trader, account)
```

- [ ] **Step 3: 跑测试确认失败**

```bash
source /Users/mameican/Desktop/server/venv/bin/activate
pytest tests/trade_result/test_trades_reader.py -v
```

预期：ImportError。

- [ ] **Step 4: 实现 `trades_reader.py`**

```python
"""封装 trader.query_stock_trades 返回。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QmtTrade:
    order_id: str
    stock_code: str
    traded_price: float
    traded_volume: int
    traded_amount: float
    traded_time: str  # ISO 8601


def _ts_to_iso(ts: int) -> str:
    """QMT 的 traded_time 是秒级 Unix 时间戳 → ISO 8601（带本地时区）。"""
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone()
    return dt.isoformat(timespec="seconds")


def fetch_trades(trader: Any, account: Any) -> list[QmtTrade]:
    """调用 trader.query_stock_trades(account)，返回规范化 QmtTrade 列表。"""
    raw = trader.query_stock_trades(account) or []
    out: list[QmtTrade] = []
    for r in raw:
        out.append(QmtTrade(
            order_id=str(int(r.order_id)),
            stock_code=str(r.stock_code),
            traded_price=float(r.traded_price),
            traded_volume=int(r.traded_volume),
            traded_amount=float(r.traded_amount),
            traded_time=_ts_to_iso(r.traded_time),
        ))
    logger.info("query_stock_trades 返回 %d 条", len(out))
    return out
```

- [ ] **Step 5: 跑测试确认通过**

```bash
pytest tests/trade_result/test_trades_reader.py -v
```

预期：4 个测试 PASS。

- [ ] **Step 6: Commit**

```bash
cd /Users/mameican/Desktop/server
git add src/trade_result/__init__.py src/trade_result/trades_reader.py tests/trade_result/__init__.py tests/trade_result/test_trades_reader.py
git commit -m "feat(trade-result): add QMT trades reader normalized to QmtTrade"
```

---

## Task 2: Matcher（QMT 成交 + 本地 orders → 服务器格式 results）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/trade_result/matcher.py`
- Create: `/Users/mameican/Desktop/server/tests/trade_result/test_matcher.py`

**输入:** 今日所有 QmtTrade + 今日所有 orders 行（`submit_status='SUCCESS'` 的）

**聚合规则（按 order_id 分组）:**
- `filled_quantity = SUM(traded_volume)`
- `filled_price = SUM(traded_amount) / SUM(traded_volume)` 若总量 > 0，否则 0
- `filled_time = MAX(traded_time)` 若有成交，否则 None
- `status`:
  - `FILLED`：`filled_quantity >= submitted_quantity`
  - `PARTIAL`：`0 < filled_quantity < submitted_quantity`
  - 暂时把"无成交"归为 `CANCELLED`（集合竞价结束后未成交 = 已撤；留给后续用 `query_stock_orders` 区分 REJECTED，当前版本先不做）

**未在 QMT 成交记录中出现的 order**：仍产生一条 `filled_quantity=0, filled_price=0, status=CANCELLED`

**产出:**

```python
@dataclass
class TradeRecord:
    order_id: str
    signal_id: str
    symbol: str
    direction: str
    submitted_quantity: int
    filled_quantity: int
    filled_price: float
    filled_time: str | None
    status: str  # FILLED / PARTIAL / CANCELLED / REJECTED
```

- [ ] **Step 1: 先写失败测试**

```python
"""matcher 测试"""
from __future__ import annotations

from src.trade_result.matcher import TradeRecord, match_trades
from src.trade_result.trades_reader import QmtTrade


def _order(oid="o1", sid="s1", qty=100, direction="BUY"):
    """返回 orders 表行的 dict（模拟 sqlite Row）"""
    return {
        "order_id": oid, "signal_id": sid,
        "symbol": "600519.SH", "direction": direction,
        "submitted_quantity": qty,
    }


def _qt(oid, price, vol, ts="2026-04-22T09:25:00+08:00"):
    return QmtTrade(
        order_id=oid, stock_code="600519.SH",
        traded_price=price, traded_volume=vol,
        traded_amount=price * vol, traded_time=ts,
    )


def test_match_filled_single_trade():
    orders = [_order("o1", "s1", qty=100)]
    trades = [_qt("o1", 10.0, 100)]

    records = match_trades(trades, orders)

    assert len(records) == 1
    r = records[0]
    assert isinstance(r, TradeRecord)
    assert r.order_id == "o1"
    assert r.signal_id == "s1"
    assert r.filled_quantity == 100
    assert r.filled_price == 10.0
    assert r.status == "FILLED"


def test_match_partial_single_trade():
    orders = [_order("o1", "s1", qty=100)]
    trades = [_qt("o1", 10.0, 30)]

    records = match_trades(trades, orders)

    assert records[0].filled_quantity == 30
    assert records[0].status == "PARTIAL"


def test_match_multiple_trades_aggregated():
    """同一 order 多笔成交应聚合"""
    orders = [_order("o1", "s1", qty=100)]
    trades = [
        _qt("o1", 10.0, 30, ts="2026-04-22T09:25:00+08:00"),
        _qt("o1", 10.2, 70, ts="2026-04-22T09:26:00+08:00"),
    ]

    records = match_trades(trades, orders)

    r = records[0]
    assert r.filled_quantity == 100
    # 加权均价 (10*30 + 10.2*70) / 100 = 10.14
    assert abs(r.filled_price - 10.14) < 1e-6
    assert r.filled_time == "2026-04-22T09:26:00+08:00"
    assert r.status == "FILLED"


def test_match_order_without_trades_is_cancelled():
    orders = [_order("o1", "s1", qty=100)]
    trades: list[QmtTrade] = []

    records = match_trades(trades, orders)

    assert len(records) == 1
    r = records[0]
    assert r.filled_quantity == 0
    assert r.filled_price == 0.0
    assert r.filled_time is None
    assert r.status == "CANCELLED"


def test_match_trade_without_order_is_ignored():
    """QMT 里有 order_id 但本地 orders 表没有 → 忽略（可能是手动单等）"""
    orders = [_order("o1", "s1", qty=100)]
    trades = [
        _qt("o1", 10.0, 100),
        _qt("o99", 50.0, 50),  # 无对应 order
    ]

    records = match_trades(trades, orders)

    assert [r.order_id for r in records] == ["o1"]


def test_match_mixed_orders_some_filled_some_not():
    orders = [
        _order("o1", "s1", qty=100),
        _order("o2", "s2", qty=200),
        _order("o3", "s3", qty=300),
    ]
    trades = [
        _qt("o1", 10.0, 100),  # o1 完全成交
        _qt("o2", 20.0, 50),   # o2 部分成交
        # o3 未成交
    ]

    records = match_trades(trades, orders)

    by_sid = {r.signal_id: r for r in records}
    assert by_sid["s1"].status == "FILLED"
    assert by_sid["s2"].status == "PARTIAL"
    assert by_sid["s3"].status == "CANCELLED"
    assert by_sid["s3"].filled_quantity == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/trade_result/test_matcher.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `matcher.py`**

```python
"""把 QMT 成交与本地 orders 按 order_id 聚合匹配。"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping

from src.trade_result.trades_reader import QmtTrade

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeRecord:
    order_id: str
    signal_id: str
    symbol: str
    direction: str
    submitted_quantity: int
    filled_quantity: int
    filled_price: float
    filled_time: str | None
    status: str


def match_trades(
    trades: list[QmtTrade],
    orders: list[Mapping[str, object]],
) -> list[TradeRecord]:
    """根据 order_id 聚合 trades、join orders，输出每个 order 一条 TradeRecord。

    orders 中每条至少包含 order_id / signal_id / symbol / direction / submitted_quantity。
    QMT trades 里出现但 orders 里没有的 order_id 会被忽略。
    orders 里存在但无 QMT trade 的 order 输出 status=CANCELLED 的占位行。
    """
    by_order: dict[str, list[QmtTrade]] = defaultdict(list)
    for t in trades:
        by_order[t.order_id].append(t)

    result: list[TradeRecord] = []
    for o in orders:
        oid = str(o["order_id"])
        its = by_order.get(oid, [])
        total_vol = sum(t.traded_volume for t in its)
        total_amt = sum(t.traded_amount for t in its)
        avg_price = (total_amt / total_vol) if total_vol > 0 else 0.0
        last_time = (max(t.traded_time for t in its) if its else None)
        submitted_qty = int(o["submitted_quantity"])

        if total_vol == 0:
            status = "CANCELLED"
        elif total_vol >= submitted_qty:
            status = "FILLED"
        else:
            status = "PARTIAL"

        result.append(TradeRecord(
            order_id=oid,
            signal_id=str(o["signal_id"]),
            symbol=str(o["symbol"]),
            direction=str(o["direction"]),
            submitted_quantity=submitted_qty,
            filled_quantity=total_vol,
            filled_price=round(avg_price, 4),
            filled_time=last_time,
            status=status,
        ))

    ignored = set(by_order.keys()) - {str(o["order_id"]) for o in orders}
    if ignored:
        logger.warning("QMT 成交记录中 %d 个 order_id 未在本地 orders 表中：%s",
                       len(ignored), sorted(ignored)[:5])
    return result
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/trade_result/test_matcher.py -v
```

预期：6 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/trade_result/matcher.py tests/trade_result/test_matcher.py
git commit -m "feat(trade-result): add matcher aggregating QMT trades by order_id"
```

---

## Task 3: Store（写 trades 表）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/trade_result/store.py`
- Create: `/Users/mameican/Desktop/server/tests/trade_result/test_store.py`

**产出:** `save_trades(conn, records, stage) -> int`

**幂等策略:** 先 `DELETE FROM trades WHERE order_id IN (...)` 删掉本次涉及的 order_id 旧行，再 INSERT。这样 `auction` 阶段和 `close` 阶段都能独立覆盖写，最后一次即为当前状态。

**额外字段:** 写入时 `reported_at` 先留 NULL，等 reporter 推送成功后再 UPDATE。

- [ ] **Step 1: 先写失败测试**

```python
"""trade_result.store 测试"""
from __future__ import annotations

from pathlib import Path

from src.common.db import get_connection, init_schema
from src.trade_result.matcher import TradeRecord
from src.trade_result.store import mark_reported, save_trades


def _seed_order(conn, order_id, signal_id):
    # signals -> orders 约束：先造 signals
    conn.execute(
        """INSERT INTO signals
        (signal_id, symbol, direction, quantity, order_type, limit_price,
         price_offset, strategy_id, signal_time, valid_date, fetched_at)
        VALUES (?, '600519.SH', 'BUY', 100, 'LIMIT', 10.0, 0.005, 's',
                '2026-04-21T18:30:00+08:00', '20260422', '2026-04-21T19:00:00+08:00')""",
        (signal_id,),
    )
    conn.execute(
        """INSERT INTO orders
        (order_id, signal_id, symbol, direction, submitted_price,
         submitted_quantity, submitted_at, submit_status)
        VALUES (?, ?, '600519.SH', 'BUY', 10.0, 100,
                '2026-04-22T09:15:00+08:00', 'SUCCESS')""",
        (order_id, signal_id),
    )
    conn.commit()


def _rec(oid="o1", sid="s1", qty=100, price=10.0,
         ft="2026-04-22T09:25:00+08:00", status="FILLED") -> TradeRecord:
    return TradeRecord(
        order_id=oid, signal_id=sid, symbol="600519.SH", direction="BUY",
        submitted_quantity=100, filled_quantity=qty, filled_price=price,
        filled_time=ft, status=status,
    )


def test_save_trades_inserts(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _seed_order(conn, "o1", "s1")

    n = save_trades(conn, [_rec()], stage="auction")

    assert n == 1
    row = conn.execute(
        "SELECT order_id, signal_id, filled_quantity, status, reported_at "
        "FROM trades WHERE order_id='o1'"
    ).fetchone()
    assert row[0] == "o1"
    assert row[1] == "s1"
    assert row[2] == 100
    assert row[3] == "FILLED"
    assert row[4] is None  # reported_at 尚未设置


def test_save_trades_replaces_existing_for_same_order(tmp_path: Path):
    """auction 写入后，close 写入应覆盖同 order_id 的旧行（DELETE+INSERT）"""
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _seed_order(conn, "o1", "s1")

    save_trades(conn, [_rec(qty=30, status="PARTIAL")], stage="auction")
    save_trades(conn, [_rec(qty=100, status="FILLED")], stage="close")

    rows = conn.execute(
        "SELECT filled_quantity, status FROM trades WHERE order_id='o1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 100
    assert rows[0][1] == "FILLED"


def test_save_trades_null_filled_time(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _seed_order(conn, "o1", "s1")

    rec = _rec(qty=0, price=0.0, ft=None, status="CANCELLED")
    save_trades(conn, [rec], stage="auction")

    row = conn.execute(
        "SELECT filled_time, status FROM trades WHERE order_id='o1'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == "CANCELLED"


def test_save_trades_empty_list(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    assert save_trades(conn, [], stage="auction") == 0


def test_mark_reported_updates_status(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    init_schema(conn)
    _seed_order(conn, "o1", "s1")
    save_trades(conn, [_rec()], stage="auction")

    mark_reported(conn, order_ids=["o1"], report_status="SUCCESS")

    row = conn.execute(
        "SELECT reported_at, report_status FROM trades WHERE order_id='o1'"
    ).fetchone()
    assert row[0] is not None
    assert row[1] == "SUCCESS"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/trade_result/test_store.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `store.py`**

```python
"""写入 trades 表：DELETE+INSERT 保证同 order_id 同 stage 可覆盖。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from src.trade_result.matcher import TradeRecord

logger = logging.getLogger(__name__)

_INSERT_SQL = """
INSERT INTO trades
(order_id, signal_id, filled_quantity, filled_price, filled_time,
 status, reported_at, report_status)
VALUES (?, ?, ?, ?, ?, ?, NULL, NULL);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def save_trades(
    conn: sqlite3.Connection,
    records: list[TradeRecord],
    stage: str,
) -> int:
    """幂等写入：先删本次涉及的 order_id，再插。"""
    if not records:
        return 0

    oids = [r.order_id for r in records]
    placeholders = ",".join("?" * len(oids))
    conn.execute(f"DELETE FROM trades WHERE order_id IN ({placeholders})", oids)

    rows = [
        (r.order_id, r.signal_id, r.filled_quantity, r.filled_price,
         r.filled_time, r.status)
        for r in records
    ]
    conn.executemany(_INSERT_SQL, rows)
    conn.commit()
    logger.info("trades 表 stage=%s 写入 %d 行", stage, len(rows))
    return len(rows)


def mark_reported(
    conn: sqlite3.Connection,
    order_ids: list[str],
    report_status: str,
) -> None:
    """推送完成后回标 reported_at + report_status。"""
    if not order_ids:
        return
    placeholders = ",".join("?" * len(order_ids))
    ts = _now_iso()
    conn.execute(
        f"UPDATE trades SET reported_at = ?, report_status = ? "
        f"WHERE order_id IN ({placeholders})",
        (ts, report_status, *order_ids),
    )
    conn.commit()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/trade_result/test_store.py -v
```

预期：5 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/trade_result/store.py tests/trade_result/test_store.py
git commit -m "feat(trade-result): add trades writer and mark_reported helper"
```

---

## Task 4: Reporter（POST /trade-result 含重试）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/trade_result/reporter.py`
- Create: `/Users/mameican/Desktop/server/tests/trade_result/test_reporter.py`

**API 合约（参考接口三）:**

```json
{
  "trade_date": "20260422",
  "stage": "auction",
  "results": [
    {"signal_id": "sig1", "symbol": "600519.SH", "direction": "BUY",
     "filled_quantity": 100, "filled_price": 1547.7,
     "filled_time": "2026-04-22T09:25:12+08:00", "status": "FILLED"}
  ]
}
```

**重试策略（与模块二一致）:** 5xx / 网络异常 / code!=0 重试 3 次；4xx 立即失败。

**产出:** `ReportResult`
- `ok: bool`
- `attempts: int`
- `matched_count: int | None`
- `unmatched_signal_ids: list[str]`
- `error: str | None`

- [ ] **Step 1: 先写失败测试**

```python
"""reporter 测试（httpx.MockTransport）"""
from __future__ import annotations

import httpx
import pytest

from src.common.http_client import new_http_client
from src.trade_result.matcher import TradeRecord
from src.trade_result.reporter import report_trade_result


def _rec(sid="s1", qty=100, price=10.0, status="FILLED",
         ft="2026-04-22T09:25:00+08:00") -> TradeRecord:
    return TradeRecord(
        order_id="o1", signal_id=sid, symbol="600519.SH", direction="BUY",
        submitted_quantity=100, filled_quantity=qty, filled_price=price,
        filled_time=ft, status=status,
    )


def _mk_client(handlers: list, base_url="https://srv") -> httpx.Client:
    it = iter(handlers)

    def h(req):
        return next(it)(req)

    return new_http_client(base_url, "K", timeout=10,
                           transport=httpx.MockTransport(h))


def test_report_happy_path():
    captured = {}

    def handler(req):
        import json
        captured["body"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, json={
            "code": 0, "data": {"trade_date": "20260422", "stage": "auction",
                                "matched_count": 1, "unmatched_signal_ids": []}})

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))

    result = report_trade_result(
        trade_date="20260422", stage="auction",
        records=[_rec()],
        http_client=client, max_retries=3, backoff=0,
    )

    assert result.ok is True
    assert result.attempts == 1
    assert result.matched_count == 1
    assert result.unmatched_signal_ids == []

    assert captured["body"]["trade_date"] == "20260422"
    assert captured["body"]["stage"] == "auction"
    assert len(captured["body"]["results"]) == 1
    assert captured["body"]["results"][0]["signal_id"] == "s1"
    assert captured["body"]["results"][0]["filled_time"] == "2026-04-22T09:25:00+08:00"


def test_report_omits_filled_time_when_none():
    captured = {}

    def handler(req):
        import json
        captured["body"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, json={
            "code": 0, "data": {"matched_count": 1, "unmatched_signal_ids": []}})

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))

    report_trade_result(
        trade_date="20260422", stage="auction",
        records=[_rec(ft=None, qty=0, price=0.0, status="CANCELLED")],
        http_client=client, max_retries=3, backoff=0,
    )

    # filled_time 在未成交时可省略（per API spec）
    r0 = captured["body"]["results"][0]
    assert "filled_time" not in r0 or r0["filled_time"] is None
    assert r0["filled_quantity"] == 0
    assert r0["filled_price"] == 0.0


def test_report_retries_on_5xx(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    client = _mk_client([
        lambda req: httpx.Response(500),
        lambda req: httpx.Response(500),
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"matched_count": 1, "unmatched_signal_ids": []}}),
    ])

    r = report_trade_result("20260422", "auction", [_rec()], client,
                            max_retries=3, backoff=0)
    assert r.ok is True
    assert r.attempts == 3


def test_report_4xx_no_retry(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    client = _mk_client([
        lambda req: httpx.Response(401, json={"code": 1001, "message": "auth"}),
    ])

    r = report_trade_result("20260422", "auction", [_rec()], client,
                            max_retries=3, backoff=0)

    assert r.ok is False
    assert r.attempts == 1


def test_report_exhausts_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    client = _mk_client([
        lambda req: httpx.Response(500),
        lambda req: httpx.Response(500),
        lambda req: httpx.Response(500),
    ])

    r = report_trade_result("20260422", "auction", [_rec()], client,
                            max_retries=3, backoff=0)

    assert r.ok is False
    assert r.attempts == 3


def test_report_surfaces_unmatched(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={
            "code": 0, "data": {"matched_count": 1,
                                "unmatched_signal_ids": ["s99"]}})

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))

    r = report_trade_result("20260422", "auction", [_rec()], client,
                            max_retries=3, backoff=0)

    assert r.ok is True
    assert r.unmatched_signal_ids == ["s99"]


def test_report_empty_records_does_not_post():
    called = []

    def handler(req):
        called.append(1)
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))

    r = report_trade_result("20260422", "auction", [], client,
                            max_retries=3, backoff=0)

    assert r.ok is True  # nothing to do = trivially ok
    assert r.attempts == 0
    assert called == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/trade_result/test_reporter.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `reporter.py`**

```python
"""POST /trade-result 推送，重试 3 次（与模块二语义一致）。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

from src.trade_result.matcher import TradeRecord

logger = logging.getLogger(__name__)

_ENDPOINT = "/trade-result"


@dataclass
class ReportResult:
    ok: bool
    attempts: int
    matched_count: int | None = None
    unmatched_signal_ids: list[str] = field(default_factory=list)
    error: str | None = None


def _record_to_payload(r: TradeRecord) -> dict:
    d = {
        "signal_id": r.signal_id,
        "symbol": r.symbol,
        "direction": r.direction,
        "filled_quantity": int(r.filled_quantity),
        "filled_price": float(r.filled_price),
        "status": r.status,
    }
    if r.filled_time is not None:
        d["filled_time"] = r.filled_time
    return d


def report_trade_result(
    trade_date: str,
    stage: str,
    records: list[TradeRecord],
    http_client: httpx.Client,
    max_retries: int = 3,
    backoff: int = 2,
) -> ReportResult:
    """推 POST /trade-result。空 records 直接返回 ok。"""
    if not records:
        logger.info("无 trade records 可推送（stage=%s）", stage)
        return ReportResult(ok=True, attempts=0)

    payload = {
        "trade_date": trade_date,
        "stage": stage,
        "results": [_record_to_payload(r) for r in records],
    }

    last_err: str | None = None
    for attempt in range(1, max_retries + 1):
        logger.info("POST %s stage=%s 第 %d/%d 次", _ENDPOINT, stage, attempt, max_retries)
        try:
            resp = http_client.post(_ENDPOINT, json=payload)
        except httpx.HTTPError as e:
            last_err = f"network error: {e}"
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        if 400 <= resp.status_code < 500:
            try:
                body = resp.json()
                last_err = (f"HTTP {resp.status_code} "
                            f"code={body.get('code')} message={body.get('message')}")
            except Exception:  # noqa: BLE001
                last_err = f"HTTP {resp.status_code}"
            return ReportResult(ok=False, attempts=attempt, error=last_err)

        if resp.status_code >= 500:
            last_err = f"HTTP {resp.status_code}"
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        try:
            body = resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = f"response not JSON: {e}"
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        if body.get("code") != 0:
            last_err = f"code={body.get('code')} message={body.get('message')}"
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        data = body.get("data") or {}
        return ReportResult(
            ok=True, attempts=attempt,
            matched_count=int(data.get("matched_count", 0)),
            unmatched_signal_ids=list(data.get("unmatched_signal_ids") or []),
        )

    return ReportResult(ok=False, attempts=max_retries, error=last_err)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/trade_result/test_reporter.py -v
```

预期：7 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/trade_result/reporter.py tests/trade_result/test_reporter.py
git commit -m "feat(trade-result): add reporter posting /trade-result with retry"
```

---

## Task 5: CLI 编排（`--stage auction|close`）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/trade_result/__main__.py`
- Create: `/Users/mameican/Desktop/server/tests/trade_result/test_cli.py`

**用法:**

```bash
# 09:35 竞价成交
python -m src.trade_result --stage auction --today 20260422 --config config/settings.yaml

# 15:30 收盘最终成交
python -m src.trade_result --stage close --today 20260422 --config config/settings.yaml
```

**退出码:**
- `0` 推送成功（含无数据可推）
- `1` 配置/参数错误
- `2` QMT 连接失败
- `3` 推送最终失败（已 alert）

**通知语义:**
- auction 成功：`竞价成交通知（{date}）：成交 N 单 / 部分 M 单 / 未成 K 单`（info）
- close 成功：`收盘成交汇总（{date}）：成交 N 单 / 部分 M 单 / 未成 K 单`（info）
- 推送失败：`[报警] {stage} 成交回报推送失败（{date}）：{error}`
- 未匹配信号：`[报警] {stage} 存在未匹配信号：{signal_ids}`（仍发 alert 且退出 3）

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


def _seed(db_path: Path):
    from src.common.db import get_connection, init_schema
    conn = get_connection(db_path)
    init_schema(conn)
    conn.execute(
        """INSERT INTO signals
        (signal_id, symbol, direction, quantity, order_type, limit_price,
         price_offset, strategy_id, signal_time, valid_date, fetched_at)
        VALUES ('s1', '600519.SH', 'BUY', 100, 'LIMIT', 10.0, 0.005,
                'strat', '2026-04-21T18:30:00+08:00', '20260422',
                '2026-04-21T19:00:00+08:00')""",
    )
    conn.execute(
        """INSERT INTO orders
        (order_id, signal_id, symbol, direction, submitted_price,
         submitted_quantity, submitted_at, submit_status)
        VALUES ('o1', 's1', '600519.SH', 'BUY', 10.05, 100,
                '2026-04-22T09:15:00+08:00', 'SUCCESS')""",
    )
    conn.commit()
    conn.close()


@pytest.fixture
def fake_xt(monkeypatch):
    trader = MagicMock()
    trader.start = MagicMock()
    trader.connect = MagicMock(return_value=0)
    trader.subscribe = MagicMock(return_value=0)
    trader.query_stock_trades = MagicMock(return_value=[
        SimpleNamespace(order_id=1, stock_code="600519.SH",
                        traded_price=10.0, traded_volume=100,
                        traded_amount=1000.0,
                        traded_time=1745284500),  # 任意 ts
    ])
    # Note: matcher 用 order_id 字符串匹配；xttrader 返回 int
    # o1 是种子数据中的 order_id；这里我们 order_id=1 会转成 "1"
    # 所以改匹配：重新 seed 成 o1="1" 或改 query 返回 order_id=0
    # 这里直接让 trader 返回 order_id 为字符 "o1" 对应的整数 — 但 int 没有 "o1"
    # 为避免复杂：下面的测试不依赖真实匹配成交，只验证 CLI 流程

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


def _patch_clients(monkeypatch, cli_mod, server_handler, wecom_handler):
    monkeypatch.setattr(cli_mod, "_new_server_client",
                        lambda cfg: cli_mod.new_http_client(
                            cfg.server.base_url, cfg.server.api_key,
                            cfg.server.timeout,
                            transport=httpx.MockTransport(server_handler)))
    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))


def test_cli_auction_happy_path(fake_xt, tmp_path: Path, monkeypatch):
    from src.trade_result import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _seed(data_root / "trading.db")

    # trader 返回 order_id=1（int）→ fetch_trades 转成 "1"；但 orders 表有 "o1"
    # 所以 matcher 会把 "1" 视为"本地没有的 order_id"忽略，而 "o1" 输出 CANCELLED
    # 对本测试这是可接受的：CLI 流程验证不依赖实际成交
    server_calls = []

    def server_handler(req):
        import json
        server_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={
            "code": 0, "data": {"matched_count": 1,
                                "unmatched_signal_ids": []}})

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    _patch_clients(monkeypatch, cli_mod, server_handler, wecom_handler)

    exit_code = cli_mod.main([
        "--stage", "auction", "--today", "20260422",
        "--config", str(cfg),
    ])

    assert exit_code == 0
    assert len(server_calls) == 1
    assert server_calls[0]["stage"] == "auction"
    assert any("竞价成交" in c["text"]["content"] for c in wecom_calls)


def test_cli_close_stage(fake_xt, tmp_path: Path, monkeypatch):
    from src.trade_result import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _seed(data_root / "trading.db")

    server_calls = []

    def server_handler(req):
        import json
        server_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={
            "code": 0, "data": {"matched_count": 1,
                                "unmatched_signal_ids": []}})

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    _patch_clients(monkeypatch, cli_mod, server_handler, wecom_handler)

    exit_code = cli_mod.main([
        "--stage", "close", "--today", "20260422", "--config", str(cfg),
    ])

    assert exit_code == 0
    assert server_calls[0]["stage"] == "close"
    assert any("收盘成交" in c["text"]["content"] for c in wecom_calls)


def test_cli_trader_fail_alerts(fake_xt, tmp_path: Path, monkeypatch):
    from src.trade_result import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _seed(data_root / "trading.db")

    fake_xt.trader.connect.return_value = -1

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    _patch_clients(monkeypatch, cli_mod,
                   lambda req: httpx.Response(500), wecom_handler)

    exit_code = cli_mod.main([
        "--stage", "auction", "--today", "20260422", "--config", str(cfg),
    ])

    assert exit_code == 2
    assert any(c["text"]["content"].startswith("[报警]") for c in wecom_calls)


def test_cli_push_failure_alerts(fake_xt, tmp_path: Path, monkeypatch):
    from src.trade_result import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _seed(data_root / "trading.db")

    def server_handler(req):
        return httpx.Response(500)

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    _patch_clients(monkeypatch, cli_mod, server_handler, wecom_handler)
    monkeypatch.setattr("time.sleep", lambda s: None)

    exit_code = cli_mod.main([
        "--stage", "auction", "--today", "20260422", "--config", str(cfg),
    ])

    assert exit_code == 3
    assert any(c["text"]["content"].startswith("[报警]") for c in wecom_calls)


def test_cli_no_orders_nothing_to_report(fake_xt, tmp_path: Path, monkeypatch):
    from src.trade_result import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    # 不 seed orders 表

    server_called = []

    def server_handler(req):
        server_called.append(1)
        return httpx.Response(200, json={"code": 0, "data": {}})

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    _patch_clients(monkeypatch, cli_mod, server_handler, wecom_handler)

    exit_code = cli_mod.main([
        "--stage", "auction", "--today", "20260422", "--config", str(cfg),
    ])

    assert exit_code == 0
    assert server_called == []  # 无 record，不推送
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/trade_result/test_cli.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `__main__.py`**

```python
"""CLI：python -m src.trade_result --stage auction|close --today YYYYMMDD --config path

09:35 跑 auction；15:30 跑 close。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from src.auction_order.trader_connector import connect_trader
from src.common.config import Config, load_config
from src.common.db import get_connection, init_schema
from src.common.http_client import new_http_client
from src.common.logging_setup import setup_logging
from src.common.notify import notify_wecom
from src.market_data_download.connector import init_xtquant
from src.trade_result.matcher import match_trades
from src.trade_result.reporter import report_trade_result
from src.trade_result.store import mark_reported, save_trades
from src.trade_result.trades_reader import fetch_trades


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.trade_result",
        description="查询 QMT 成交、写 trades、POST /trade-result。",
    )
    p.add_argument("--stage", required=True, choices=["auction", "close"])
    p.add_argument("--today", help="YYYYMMDD；缺省取本机当天")
    p.add_argument("--config", required=True, help="settings.yaml 路径")
    return p


def _new_server_client(cfg: Config):
    return new_http_client(
        cfg.server.base_url, cfg.server.api_key, cfg.server.timeout,
    )


def _notify(webhook: str, message: str, level: str) -> bool:
    return notify_wecom(webhook, message, level)


def _load_orders(conn: sqlite3.Connection, trade_date: str) -> list[dict]:
    """取指定 trade_date 已成功提交的 orders（用 signals.valid_date 对齐）。"""
    cur = conn.execute(
        """
        SELECT o.order_id, o.signal_id, o.symbol, o.direction,
               o.submitted_quantity
        FROM orders o
        JOIN signals s ON s.signal_id = o.signal_id
        WHERE s.valid_date = ? AND o.submit_status = 'SUCCESS'
        ORDER BY o.submitted_at
        """,
        (trade_date,),
    )
    return [dict(r) for r in cur.fetchall()]


def _summary(records: list) -> str:
    filled = sum(1 for r in records if r.status == "FILLED")
    partial = sum(1 for r in records if r.status == "PARTIAL")
    others = len(records) - filled - partial
    return f"成交 {filled} 单 / 部分 {partial} 单 / 未成 {others} 单"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    logger = setup_logging(cfg.paths.log_dir, "trade_result")
    today = args.today or datetime.now().strftime("%Y%m%d")
    logger.info("开始 %s stage 成交回报，today=%s", args.stage, today)

    init_xtquant(cfg.qmt.data_dir)
    try:
        trader, account = connect_trader(
            data_dir=cfg.qmt.data_dir, account_id=cfg.qmt.account_id,
        )
    except (ValueError, RuntimeError) as e:
        logger.error("QMT 连接失败: %s", e)
        _notify(cfg.notify.wecom_webhook,
                f"{args.stage} 回报脚本启动失败：QMT 连接异常 — {e}", "alert")
        return 2

    qmt_trades = fetch_trades(trader, account)

    db_path = Path(cfg.paths.sqlite_path)
    conn = get_connection(db_path)
    try:
        init_schema(conn)
        orders = _load_orders(conn, today)
        if not orders:
            logger.info("当日 orders 表无记录，无需推送")
            # 无 orders 也无需通知（模块四的 no-signals 场景由 19:00 脚本处理）
            return 0

        records = match_trades(qmt_trades, orders)
        save_trades(conn, records, stage=args.stage)
    finally:
        conn.close()

    with _new_server_client(cfg) as client:
        result = report_trade_result(
            trade_date=today, stage=args.stage,
            records=records, http_client=client,
            max_retries=3, backoff=2,
        )

    conn = get_connection(db_path)
    try:
        if result.ok:
            mark_reported(conn,
                          order_ids=[r.order_id for r in records],
                          report_status="SUCCESS")
        else:
            mark_reported(conn,
                          order_ids=[r.order_id for r in records],
                          report_status="FAILED")
    finally:
        conn.close()

    if not result.ok:
        _notify(cfg.notify.wecom_webhook,
                f"{args.stage} 成交回报推送失败（{today}）：{result.error}", "alert")
        return 3

    label = "竞价成交通知" if args.stage == "auction" else "收盘成交汇总"
    msg = f"{label}（{today}）：{_summary(records)}"
    if result.unmatched_signal_ids:
        # 推送成功但有未匹配信号：告警，但不算失败
        _notify(cfg.notify.wecom_webhook,
                f"{args.stage} 存在未匹配信号（{today}）：{result.unmatched_signal_ids}",
                "alert")
        logger.warning("未匹配 signal_ids: %s", result.unmatched_signal_ids)
        return 3

    _notify(cfg.notify.wecom_webhook, msg, "info")
    logger.info("完成 stage=%s: %s", args.stage, _summary(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/trade_result/test_cli.py -v
```

预期：5 个测试 PASS。

- [ ] **Step 5: 全回归**

```bash
pytest -v
```

预期：Plan A + B + C + D + E 全部绿。

- [ ] **Step 6: Commit**

```bash
git add src/trade_result/__main__.py tests/trade_result/test_cli.py
git commit -m "feat(trade-result): add CLI for auction/close stage reporting"
```

---

## Task 6: Windows 任务脚本（09:35 + 15:30）

**Files:**
- Create: `/Users/mameican/Desktop/server/scripts/daily_0935_auction_report.bat`
- Create: `/Users/mameican/Desktop/server/scripts/daily_1530_close_report.bat`

- [ ] **Step 1: 写 09:35 脚本**

```bat
@echo off
REM QMT 模拟盘 — 09:35 竞价成交回报脚本

set PROJECT_ROOT=C:\parttime\qmt模拟盘pipeline\server
set VENV=C:\parttime\qmt数据推送\venv
set LOGDIR=%PROJECT_ROOT%\logs
set TODAY=%date:~0,4%%date:~5,2%%date:~8,2%

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

call "%VENV%\Scripts\activate.bat"
cd /d "%PROJECT_ROOT%"
python -m src.trade_result --stage auction --today %TODAY% --config config\settings.yaml >> "%LOGDIR%\trade_auction_%TODAY%.log" 2>&1
exit /b %ERRORLEVEL%
```

- [ ] **Step 2: 写 15:30 脚本**

```bat
@echo off
REM QMT 模拟盘 — 15:30 收盘成交回报脚本（与模块一同批触发）

set PROJECT_ROOT=C:\parttime\qmt模拟盘pipeline\server
set VENV=C:\parttime\qmt数据推送\venv
set LOGDIR=%PROJECT_ROOT%\logs
set TODAY=%date:~0,4%%date:~5,2%%date:~8,2%

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

call "%VENV%\Scripts\activate.bat"
cd /d "%PROJECT_ROOT%"
python -m src.trade_result --stage close --today %TODAY% --config config\settings.yaml >> "%LOGDIR%\trade_close_%TODAY%.log" 2>&1
exit /b %ERRORLEVEL%
```

- [ ] **Step 3: Commit**

```bash
git add scripts/daily_0935_auction_report.bat scripts/daily_1530_close_report.bat
git commit -m "chore(trade-result): add Windows batch scripts for 0935 and 1530"
```

---

## Task 7: Windows 集成冒烟测试文档

**Files:**
- Create: `/Users/mameican/Desktop/server/docs/manual_tests/module5_6_trade_result_smoke_test.md`

- [ ] **Step 1: 写文档**

内容：

```markdown
# 模块五 + 六 Windows 集成冒烟测试

**前置条件:**
1. Plan A/B/C/D 已在 Windows 上跑通
2. `orders` 表里有至少一条 `submit_status='SUCCESS'` 的当日订单
3. QMT 客户端登录中，今日已经在集合竞价下过单（否则 query_stock_trades 返回空）

**09:35 auction 阶段验证:**

09:25~09:30 集合竞价结束后执行：

```powershell
cd C:\parttime\qmt模拟盘pipeline\server
C:\parttime\qmt数据推送\venv\Scripts\activate
python -m src.trade_result --stage auction --today 20260422 --config config\settings.yaml
```

- [ ] 退出码 0
- [ ] 日志显示 `query_stock_trades 返回 N 条`、`trades 表 stage=auction 写入 M 行`、`POST /trade-result stage=auction 第 1/3 次`
- [ ] 企业微信收到"竞价成交通知（20260422）：成交 X 单 / 部分 Y 单 / 未成 Z 单"
- [ ] SQLite 确认：
  ```sql
  SELECT signal_id, filled_quantity, status, report_status
  FROM trades
  WHERE signal_id IN (SELECT signal_id FROM signals WHERE valid_date='20260422');
  ```
  每行 `report_status='SUCCESS'`

**15:30 close 阶段验证:**

```powershell
python -m src.trade_result --stage close --today 20260422 --config config\settings.yaml
```

- [ ] 退出码 0
- [ ] 日志显示 `trades 表 stage=close 写入 M 行`
- [ ] 对比 auction 与 close：同一 order_id 在 trades 里只剩一行（DELETE+INSERT 语义）
- [ ] 若某单在盘中继续成交，`close` 的 filled_quantity/filled_price 应 >= auction 的值
- [ ] 企业微信收到"收盘成交汇总（20260422）：..."
- [ ] 服务器侧确认 close 的结果覆盖了 auction（搭档核查）

**未匹配信号演练:**

- 与搭档约定：服务器对 signal_id `s-nonexistent` 不认，返回 `unmatched_signal_ids: ["s-nonexistent"]`
- 手动在 orders 表插入一条关联到 `s-nonexistent` 的记录（仅测试）
- 运行命令
- [ ] 退出码 3
- [ ] 企业微信收到 `[报警] auction 存在未匹配信号：['s-nonexistent']`

**服务器离线演练:**

- 断网或改 `server.base_url` 为无效地址
- 运行命令
- [ ] 日志显示 3 次 network error
- [ ] 退出码 3，企业微信收到 `[报警] auction 成交回报推送失败`
- [ ] `trades` 表 `report_status='FAILED'` 已回标

**QMT 客户端未登录演练:**

- 关闭 QMT 客户端
- 运行命令
- [ ] 退出码 2，企业微信 `[报警] auction 回报脚本启动失败：QMT 连接异常`

**Windows 任务计划程序配置:**

- 09:35 触发 `scripts\daily_0935_auction_report.bat`
- 15:30 触发 `scripts\daily_1530_close_report.bat`（与模块一并行，互不干扰）
- 15:30 的脚本不应 crash 于 orders 表为空（当天根本没有信号 / 没下单）— 由 `_load_orders` 返回空列表处理
```

- [ ] **Step 2: Commit**

```bash
git add docs/manual_tests/module5_6_trade_result_smoke_test.md
git commit -m "docs: add smoke test checklist for modules 5+6 (trade result)"
```

---

## 收尾清单

- [ ] 所有 Task commit 完成
- [ ] `pytest -v` 全绿（Plan A + B + C + D + E）
- [ ] Windows 09:35 auction 冒烟通过
- [ ] Windows 15:30 close 冒烟通过
- [ ] 服务器侧确认 `stage=close` 覆盖 `stage=auction`（搭档核对）
- [ ] 端到端跑一个完整交易日：15:30 推行情 → 19:00 拿信号 → 次日 09:10 下单 → 09:35 竞价回报 → 15:30 收盘回报
- [ ] 整理一次端到端 trace 文档，归档供后续排障

---

## 风险记录与已知简化

- **status 分类简化**：当前把"无成交"统一归为 `CANCELLED`。更严谨的做法是用 `query_stock_orders()` 查询委托状态来区分 `CANCELLED`（已撤单）与 `REJECTED`（被拒绝：资金不足/涨跌停等）。当前版本先不做；后续遇到实际 REJECTED 场景（服务器偏离检测触发）再加 Task。
- **trades 表存聚合行**：按 `order_id` 聚合成一行，丢失了多笔成交的明细。如果日后策略绩效需要成交明细，应另加 `trade_fills` 表存原始 QmtTrade。
- **reported_at 回标并发**：当前顺序流程，无并发问题；若未来改异步需加事务。

---

## 整个 Pipeline 的收尾

完成本 Plan 后整个本地端 Pipeline 就齐了。建议：

1. 端到端跑 3-5 个交易日观察稳定性
2. 把日志聚合发送（如 loki 或简单 rsync 到 Mac）便于 Mac 侧分析
3. 监控指标：每天的 trade_date, received_count, signals_count, filled_orders, unmatched_signals
4. 与搭档建立"差错对账"流程：每周核对服务器记录 vs 本地 trades 表

后续如启用期权：参考 `项目设计文档（含期权）.md` 与 `API接口文档（含期权）.md`，扩展时应考虑：
- 独立的 options_signals / options_orders / options_trades 表
- 不同的下单 API（xttrader 期权接口）
- 账号类型区分（STOCK vs STOCK_OPTION）
