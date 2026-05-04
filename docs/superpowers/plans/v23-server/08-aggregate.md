# Plan 08: Aggregate — 信号归集引擎

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 把同一 `(account_group, symbol, direction)` 下的多条 PASS 信号合并为一条订单：quantity 求和；BUY 取最高 limit_price，SELL 取最低 limit_price。同时输出 order→signal 映射表（拆单按比例分摊用）。

**Architecture:** 纯函数服务 `AggregateService`。无 I/O。输入预 tag 过 `account_group + signal_id` 的信号列表 + `valid_date`，输出 `(orders[], mappings[])`。`order_id` 用 `uuid4().hex`。

**Files:**
- `v2.3/server/app/services/aggregate.py` (NEW)
- `v2.3/server/tests/unit/test_aggregate.py` (NEW)

---

## Task 1: AggregateService + 单测

### `app/services/aggregate.py`

```python
"""信号归集引擎。

合并规则：
- 分组键: (account_group, symbol, direction)
- quantity: 求和
- limit_price: BUY → max(reference × (1+offset))
              SELL → min(reference × (1+offset))
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import NamedTuple

from app.strategy.base import RawSignal


@dataclass(frozen=True)
class TaggedSignal:
    """RawSignal 经过 instance 路由后带 account_group + signal_id 的版本。"""
    signal_id: str          # 唯一，对应 raw_signals 表的 signal_id
    account_group: str
    raw: RawSignal


@dataclass(frozen=True)
class AggregatedOrder:
    """归集后的订单。"""
    order_id: str           # UUID hex
    account_group: str
    symbol: str
    direction: str
    quantity: int
    limit_price: float
    valid_date: str


class OrderSignalMapping(NamedTuple):
    order_id: str
    signal_id: str
    signal_quantity: int


@dataclass
class AggregateResult:
    orders: list[AggregatedOrder] = field(default_factory=list)
    mappings: list[OrderSignalMapping] = field(default_factory=list)


class AggregateService:
    """信号归集。无 I/O，纯计算。"""

    @staticmethod
    def _signal_limit_price(s: RawSignal) -> float:
        return float(s.reference_price) * (1.0 + float(s.price_offset))

    def aggregate(
        self,
        signals: list[TaggedSignal],
        valid_date: str,
    ) -> AggregateResult:
        """按 (account_group, symbol, direction) 归集，输出订单 + 映射。"""
        if not signals:
            return AggregateResult()

        # 分组
        groups: dict[tuple[str, str, str], list[TaggedSignal]] = defaultdict(list)
        for ts in signals:
            key = (ts.account_group, ts.raw.symbol, ts.raw.direction)
            groups[key].append(ts)

        result = AggregateResult()
        for (account_group, symbol, direction), members in groups.items():
            order_id = uuid.uuid4().hex

            # 数量求和
            total_qty = sum(m.raw.quantity for m in members)

            # limit_price 聚合
            prices = [self._signal_limit_price(m.raw) for m in members]
            if direction == "BUY":
                limit = max(prices)
            else:  # SELL
                limit = min(prices)

            result.orders.append(AggregatedOrder(
                order_id=order_id,
                account_group=account_group,
                symbol=symbol,
                direction=direction,
                quantity=total_qty,
                limit_price=round(limit, 4),
                valid_date=valid_date,
            ))
            for m in members:
                result.mappings.append(OrderSignalMapping(
                    order_id=order_id,
                    signal_id=m.signal_id,
                    signal_quantity=m.raw.quantity,
                ))

        return result
```

### `tests/unit/test_aggregate.py`

```python
"""AggregateService 单元测试"""
from app.services.aggregate import (
    AggregateService, AggregatedOrder, OrderSignalMapping, TaggedSignal,
)
from app.strategy.base import RawSignal


def _ts(signal_id: str, account_group: str = "real_A",
        symbol: str = "600519.SH", direction: str = "BUY",
        qty: int = 100, ref: float = 10.0, off: float = 0.005) -> TaggedSignal:
    return TaggedSignal(
        signal_id=signal_id,
        account_group=account_group,
        raw=RawSignal(symbol=symbol, direction=direction, quantity=qty,
                      reference_price=ref, price_offset=off),
    )


def test_aggregate_empty_returns_empty():
    svc = AggregateService()
    r = svc.aggregate([], valid_date="20260430")
    assert r.orders == []
    assert r.mappings == []


def test_aggregate_single_signal_creates_one_order():
    svc = AggregateService()
    r = svc.aggregate([_ts("s1")], valid_date="20260430")
    assert len(r.orders) == 1
    assert len(r.mappings) == 1
    assert r.orders[0].account_group == "real_A"
    assert r.orders[0].symbol == "600519.SH"
    assert r.orders[0].direction == "BUY"
    assert r.orders[0].quantity == 100
    assert r.orders[0].valid_date == "20260430"
    # limit_price = 10 * 1.005 = 10.05
    assert abs(r.orders[0].limit_price - 10.05) < 1e-6
    assert r.mappings[0].signal_id == "s1"
    assert r.mappings[0].signal_quantity == 100
    assert r.mappings[0].order_id == r.orders[0].order_id


def test_aggregate_two_signals_same_key_merges():
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", qty=100, ref=10.0, off=0.005),
        _ts("s2", qty=200, ref=10.0, off=0.005),
    ], valid_date="20260430")
    assert len(r.orders) == 1
    assert r.orders[0].quantity == 300   # 求和
    assert len(r.mappings) == 2          # 两条信号都映射
    assert {m.signal_id for m in r.mappings} == {"s1", "s2"}
    # 同一订单 id
    assert all(m.order_id == r.orders[0].order_id for m in r.mappings)


def test_aggregate_buy_takes_max_limit_price():
    """BUY 取最高出价（最激进）。"""
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", qty=100, ref=10.0, off=0.003),  # limit=10.03
        _ts("s2", qty=100, ref=10.0, off=0.005),  # limit=10.05  ← 最高
        _ts("s3", qty=100, ref=10.0, off=0.001),  # limit=10.01
    ], valid_date="20260430")
    assert len(r.orders) == 1
    assert abs(r.orders[0].limit_price - 10.05) < 1e-6


def test_aggregate_sell_takes_min_limit_price():
    """SELL 取最低要价（最激进）。"""
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", direction="SELL", qty=100, ref=10.0, off=-0.001),  # 9.99
        _ts("s2", direction="SELL", qty=100, ref=10.0, off=-0.005),  # 9.95 ← 最低
        _ts("s3", direction="SELL", qty=100, ref=10.0, off=-0.003),  # 9.97
    ], valid_date="20260430")
    assert len(r.orders) == 1
    assert abs(r.orders[0].limit_price - 9.95) < 1e-6


def test_aggregate_different_account_groups_separate():
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", account_group="real_A"),
        _ts("s2", account_group="real_B"),
    ], valid_date="20260430")
    assert len(r.orders) == 2
    groups = {o.account_group for o in r.orders}
    assert groups == {"real_A", "real_B"}


def test_aggregate_different_symbols_separate():
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", symbol="600519.SH"),
        _ts("s2", symbol="000001.SZ"),
    ], valid_date="20260430")
    assert len(r.orders) == 2


def test_aggregate_different_directions_separate():
    svc = AggregateService()
    r = svc.aggregate([
        _ts("s1", direction="BUY"),
        # SELL 同标的同账户组：分开两条订单
        TaggedSignal(
            signal_id="s2", account_group="real_A",
            raw=RawSignal(symbol="600519.SH", direction="SELL",
                          quantity=100, reference_price=10.0, price_offset=-0.005),
        ),
    ], valid_date="20260430")
    assert len(r.orders) == 2
    dirs = {o.direction for o in r.orders}
    assert dirs == {"BUY", "SELL"}


def test_aggregate_order_id_is_uuid_hex():
    svc = AggregateService()
    r = svc.aggregate([_ts("s1")], valid_date="20260430")
    oid = r.orders[0].order_id
    assert len(oid) == 32           # uuid4().hex
    assert all(c in "0123456789abcdef" for c in oid)


def test_aggregate_complex_scenario():
    """多账户组 + 多标的 + 同标的多策略归集 + 买卖分开。"""
    svc = AggregateService()
    r = svc.aggregate([
        # real_A 的 momentum + mean_rev 都买茅台
        _ts("s1", account_group="real_A", symbol="600519.SH", qty=100, off=0.003),
        _ts("s2", account_group="real_A", symbol="600519.SH", qty=200, off=0.005),
        # real_A 的 mean_rev 卖平安
        TaggedSignal("s3", "real_A", RawSignal(
            symbol="000001.SZ", direction="SELL", quantity=300,
            reference_price=14.0, price_offset=-0.005,
        )),
        # real_B 的 momentum 买茅台
        _ts("s4", account_group="real_B", symbol="600519.SH", qty=500, off=0.004),
    ], valid_date="20260430")

    # 期望 3 条订单
    assert len(r.orders) == 3
    # 期望 4 条映射（每条信号一行）
    assert len(r.mappings) == 4

    # real_A + 600519.SH + BUY 应聚合为 300 股，limit_price=10.05
    a_buy = next(o for o in r.orders
                 if o.account_group == "real_A"
                 and o.symbol == "600519.SH"
                 and o.direction == "BUY")
    assert a_buy.quantity == 300
    assert abs(a_buy.limit_price - 10.05) < 1e-6

    # real_A + 000001.SZ + SELL 单独一条
    a_sell = next(o for o in r.orders
                  if o.account_group == "real_A"
                  and o.symbol == "000001.SZ")
    assert a_sell.quantity == 300
    assert a_sell.direction == "SELL"

    # real_B + 600519.SH + BUY 单独一条
    b_buy = next(o for o in r.orders if o.account_group == "real_B")
    assert b_buy.quantity == 500
```

### 验证 + Commit

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest -v   # 期望 91 + 10 = 101 PASS
```

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/app/services/aggregate.py \
        v2.3/server/tests/unit/test_aggregate.py
git commit -m "feat(server): add AggregateService merging signals into orders"
```

---

## 收尾

- [ ] 101 PASS
- [ ] 1 commit

---

## 后续 plan

Plan 09: orders_queue + GET /orders 真实业务（拼装 storage 层 + 连 endpoint）
