# Plan 10: Settlement + POST /trade-result 真实业务

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 接收 client 推送的成交回报，把每个 order 的成交量按 `signal_quantity` 比例拆分到原始信号，更新对应实例的虚拟现金/持仓；标记订单状态。完成后第 3 个端点真实可用。

**Architecture:**
- `largest_remainder_split(total, weights)` — 最大余数法拆单纯函数
- `SettlementService` — 接收 results → 写 trades + 拆单 + 更新 instance_state + 标记 order status
- 容错：order_id 找不到 → 收集到 `unmatched_order_ids`；raw_signals 缺失 → 记录但不 crash

**Files:**
- `v2.3/server/app/services/settlement.py` (NEW)
- `v2.3/server/app/api/trade_result.py` (MODIFY，替换 stub)
- `v2.3/server/app/dependencies.py` (MODIFY，加 settlement service factory)
- `v2.3/server/tests/unit/test_settlement.py` (NEW)
- `v2.3/server/tests/unit/test_api_trade_result.py` (MODIFY，加 e2e)

---

## Task 1: SettlementService + 单测

### `app/services/settlement.py`

```python
"""成交回报处理：拆单 + 更新虚拟账本 + 标记订单状态。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select

from app.models import InstanceState, Order, OrderSignalMap, RawSignal, Trade
from app.schemas.trade_result import TradeResult, TradeResultResponseData

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def largest_remainder_split(total: int, weights: list[int]) -> list[int]:
    """最大余数法把 total 按 weights 比例拆为整数列表（保证 sum == total）。

    若 sum(weights) == 0 或 total == 0，返回全 0。

    Examples:
        >>> largest_remainder_split(350, [100, 200, 300])
        [58, 117, 175]   # sum = 350
    """
    if total == 0 or sum(weights) == 0:
        return [0] * len(weights)
    sw = sum(weights)
    raw = [total * w / sw for w in weights]
    floors = [int(x) for x in raw]
    remainder = total - sum(floors)
    if remainder == 0:
        return floors
    fractional = [(raw[i] - floors[i], i) for i in range(len(raw))]
    fractional.sort(key=lambda t: -t[0])  # 余数从大到小
    for k in range(remainder):
        floors[fractional[k][1]] += 1
    return floors


class SettlementService:
    """成交回报处理服务。"""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def settle(
        self,
        trade_date: str,
        results: list[TradeResult],
    ) -> TradeResultResponseData:
        """处理一批成交回报。"""
        matched = 0
        unmatched: list[str] = []

        with self.session_factory() as session:
            for result in results:
                order = session.get(Order, result.order_id)
                if order is None:
                    unmatched.append(result.order_id)
                    continue

                # 写 trades 表
                session.add(Trade(
                    order_id=result.order_id,
                    filled_quantity=result.filled_quantity,
                    filled_price=result.filled_price,
                    filled_time=result.filled_time,
                    status=result.status,
                    received_at=_now_iso(),
                ))

                # 拆单 + 更新虚拟账本（仅在有成交时）
                if result.filled_quantity > 0:
                    self._split_and_update_state(
                        session, order, result.filled_quantity, result.filled_price,
                    )

                # 标记订单状态
                order.status = result.status
                matched += 1

            session.commit()

        return TradeResultResponseData(
            trade_date=trade_date,
            matched_count=matched,
            unmatched_order_ids=unmatched,
        )

    # ── 内部 ────────────────────────────────────────────────────────────
    def _split_and_update_state(
        self,
        session,
        order: Order,
        filled_qty: int,
        filled_price: float,
    ) -> None:
        # 取该 order 的所有 (signal_id, signal_quantity)
        mappings = session.execute(
            select(OrderSignalMap).where(OrderSignalMap.order_id == order.order_id)
        ).scalars().all()
        if not mappings:
            logger.warning(
                "order %s 无 order_signal_map 记录，跳过拆单更新",
                order.order_id,
            )
            return

        # 拆分
        weights = [m.signal_quantity for m in mappings]
        splits = largest_remainder_split(filled_qty, weights)

        # 对每条 signal 找 instance_id 然后更新虚拟账本
        for m, split_qty in zip(mappings, splits):
            if split_qty == 0:
                continue
            sig = session.get(RawSignal, m.signal_id)
            if sig is None:
                logger.warning(
                    "signal_id=%s 在 raw_signals 中不存在，跳过虚拟账本更新",
                    m.signal_id,
                )
                continue

            inst = session.get(InstanceState, sig.instance_id)
            if inst is None:
                logger.warning(
                    "instance_id=%s 在 instance_state 中不存在，自动创建",
                    sig.instance_id,
                )
                inst = InstanceState(
                    instance_id=sig.instance_id,
                    virtual_cash=0.0,
                    virtual_positions={},
                    last_update=_now_iso(),
                )
                session.add(inst)
                # 必须 flush 才能 get 出来
                session.flush()

            # 复制 dict（SQLAlchemy mutable JSON 需要新对象）
            positions = dict(inst.virtual_positions or {})
            sym = order.symbol
            cash_delta = filled_price * split_qty
            if order.direction == "BUY":
                inst.virtual_cash = inst.virtual_cash - cash_delta
                positions[sym] = positions.get(sym, 0) + split_qty
            else:  # SELL
                inst.virtual_cash = inst.virtual_cash + cash_delta
                new_qty = positions.get(sym, 0) - split_qty
                if new_qty <= 0:
                    positions.pop(sym, None)
                else:
                    positions[sym] = new_qty
            inst.virtual_positions = positions
            inst.last_update = _now_iso()
```

### `tests/unit/test_settlement.py`

```python
"""SettlementService + largest_remainder_split 测试"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState, Order, OrderSignalMap, RawSignal, Trade
from app.schemas.trade_result import TradeResult
from app.services.settlement import SettlementService, largest_remainder_split


# ── 拆单纯函数 ────────────────────────────────────────────────────────
def test_split_zero_total():
    assert largest_remainder_split(0, [10, 20, 30]) == [0, 0, 0]


def test_split_zero_weights():
    assert largest_remainder_split(100, [0, 0]) == [0, 0]


def test_split_perfect_division():
    assert largest_remainder_split(300, [100, 200]) == [100, 200]


def test_split_with_remainder():
    """350 split by [100, 200, 300]: floor=[58,116,175] sum=349, remainder=1
    fractional parts: [.33, .67, .0]; max is index 1 → 117"""
    result = largest_remainder_split(350, [100, 200, 300])
    assert sum(result) == 350
    assert result == [58, 117, 175]


def test_split_total_smaller_than_weights():
    result = largest_remainder_split(7, [10, 20, 30])
    assert sum(result) == 7


# ── Service ──────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    return make_session_factory(engine)


def _seed(sf, *, with_signals: bool = True, with_state: bool = True):
    """种一个完整数据：1 个 order，2 条 mapping，2 条 raw_signal，1 个 instance_state。"""
    with sf() as s:
        s.add(Order(
            order_id="oid1", account_group="real_A", symbol="600519.SH",
            direction="BUY", quantity=300, limit_price=10.05,
            valid_date="20260430", status="PENDING", created_at=_now(),
        ))
        s.add(OrderSignalMap(order_id="oid1", signal_id="s1", signal_quantity=100))
        s.add(OrderSignalMap(order_id="oid1", signal_id="s2", signal_quantity=200))

        if with_signals:
            for sid, iid, qty in [("s1", "real_A_m", 100), ("s2", "real_A_r", 200)]:
                s.add(RawSignal(
                    signal_id=sid, instance_id=iid, symbol="600519.SH",
                    direction="BUY", quantity=qty, reference_price=10.0,
                    price_offset=0.005, limit_price=10.05,
                    valid_date="20260430", signal_time=_now(),
                    precheck_status="PASS",
                ))

        if with_state:
            s.add(InstanceState(instance_id="real_A_m", virtual_cash=1_000_000.0,
                                virtual_positions={}, last_update=_now()))
            s.add(InstanceState(instance_id="real_A_r", virtual_cash=2_000_000.0,
                                virtual_positions={}, last_update=_now()))
        s.commit()


def test_settle_unknown_order_in_unmatched(tmp_path: Path):
    sf = _factory(tmp_path)
    svc = SettlementService(session_factory=sf)
    resp = svc.settle("20260430", [
        TradeResult(order_id="ghost", filled_quantity=100, filled_price=10.0,
                    status="FILLED"),
    ])
    assert resp.matched_count == 0
    assert resp.unmatched_order_ids == ["ghost"]


def test_settle_writes_trade_record(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed(sf)
    svc = SettlementService(session_factory=sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.05,
                    filled_time="2026-04-30T09:25:00+08:00", status="FILLED"),
    ])
    with sf() as s:
        trades = s.query(Trade).filter_by(order_id="oid1").all()
        assert len(trades) == 1
        assert trades[0].filled_quantity == 300
        assert trades[0].status == "FILLED"


def test_settle_marks_order_status(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed(sf)
    svc = SettlementService(session_factory=sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=200, filled_price=10.0,
                    status="PARTIAL"),
    ])
    with sf() as s:
        assert s.get(Order, "oid1").status == "PARTIAL"


def test_settle_buy_updates_virtual_state_proportionally(tmp_path: Path):
    """成交 300 股全部 fill；按 100:200 比例拆 → 100/200 给 m/r 实例。"""
    sf = _factory(tmp_path)
    _seed(sf)
    svc = SettlementService(session_factory=sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        r = s.get(InstanceState, "real_A_r")
        # m 拿到 100 股 * 10 = 1000 现金减少
        assert m.virtual_cash == 1_000_000.0 - 1000.0
        assert m.virtual_positions == {"600519.SH": 100}
        # r 拿到 200 股 * 10 = 2000 现金减少
        assert r.virtual_cash == 2_000_000.0 - 2000.0
        assert r.virtual_positions == {"600519.SH": 200}


def test_settle_sell_updates_virtual_state_proportionally(tmp_path: Path):
    """同上但是 SELL：现金增加，持仓减少。"""
    sf = _factory(tmp_path)
    # 改造 seed：order 是 SELL，instance 已经有持仓
    _seed(sf, with_state=False)
    with sf() as s:
        s.get(Order, "oid1").direction = "SELL"
        s.query(RawSignal).filter_by(signal_id="s1").update({"direction": "SELL"})
        s.query(RawSignal).filter_by(signal_id="s2").update({"direction": "SELL"})
        s.add(InstanceState(instance_id="real_A_m", virtual_cash=0.0,
                            virtual_positions={"600519.SH": 100},
                            last_update=_now()))
        s.add(InstanceState(instance_id="real_A_r", virtual_cash=0.0,
                            virtual_positions={"600519.SH": 200},
                            last_update=_now()))
        s.commit()

    svc = SettlementService(session_factory=sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        r = s.get(InstanceState, "real_A_r")
        # m 卖 100 股 → 现金 +1000, 持仓 = 0 → key 应被移除
        assert m.virtual_cash == 1000.0
        assert m.virtual_positions == {}
        # r 卖 200 股 → 现金 +2000, 持仓 = 0 → key 应被移除
        assert r.virtual_cash == 2000.0
        assert r.virtual_positions == {}


def test_settle_zero_filled_skips_state_update(tmp_path: Path):
    """filled_quantity=0 (CANCELLED) 不应改虚拟账本。"""
    sf = _factory(tmp_path)
    _seed(sf)
    svc = SettlementService(session_factory=sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=0, filled_price=0.0,
                    status="CANCELLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        assert m.virtual_cash == 1_000_000.0   # 没变
        assert m.virtual_positions == {}        # 没变


def test_settle_missing_raw_signal_warns_but_continues(tmp_path: Path):
    """raw_signals 缺失不应 crash。"""
    sf = _factory(tmp_path)
    _seed(sf, with_signals=False)
    svc = SettlementService(session_factory=sf)
    resp = svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=300, filled_price=10.0,
                    status="FILLED"),
    ])
    assert resp.matched_count == 1   # order 找到了


def test_settle_partial_filled_largest_remainder(tmp_path: Path):
    """成交 350 股（如果 order 实际只下 300 股，这是不太合理的；但测试拆分逻辑）。"""
    sf = _factory(tmp_path)
    _seed(sf)
    svc = SettlementService(session_factory=sf)
    svc.settle("20260430", [
        TradeResult(order_id="oid1", filled_quantity=350, filled_price=10.0,
                    status="FILLED"),
    ])
    with sf() as s:
        m = s.get(InstanceState, "real_A_m")
        r = s.get(InstanceState, "real_A_r")
        # weights [100, 200]; 350 split: m→117, r→233
        assert m.virtual_positions["600519.SH"] == 117
        assert r.virtual_positions["600519.SH"] == 233
        # 总合 350 ✓
```

---

## Task 2: 接入 endpoint + e2e

### `app/dependencies.py` —— 末尾追加

```python
from app.services.settlement import SettlementService


def get_settlement_service(
    sf: sessionmaker = Depends(get_session_factory),
) -> SettlementService:
    return SettlementService(session_factory=sf)
```

### `app/api/trade_result.py` —— 替换 stub

```python
"""POST /trade-result — 真实业务：拆单更新虚拟账本 + 标记订单状态。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import verify_api_key
from app.dependencies import get_settlement_service
from app.schemas.common import APIResponse
from app.schemas.trade_result import TradeResultRequest, TradeResultResponseData
from app.services.settlement import SettlementService

router = APIRouter()


@router.post(
    "/trade-result",
    response_model=APIResponse[TradeResultResponseData],
    dependencies=[Depends(verify_api_key)],
)
async def push_trade_result(
    req: TradeResultRequest,
    service: SettlementService = Depends(get_settlement_service),
):
    data = service.settle(trade_date=req.trade_date, results=req.results)
    return APIResponse[TradeResultResponseData](
        code=0,
        message="ok",
        data=data,
    )
```

### `tests/unit/test_api_trade_result.py` —— 末尾追加 e2e

```python
def test_post_trade_result_unknown_order_returns_unmatched(client):
    """e2e: 未知 order_id 应被返回到 unmatched_order_ids。"""
    r = client.post("/trade-result", headers=_AUTH, json={
        "trade_date": "20260430",
        "results": [
            {"order_id": "ghost", "filled_quantity": 100, "filled_price": 10.0,
             "filled_time": "2026-04-30T09:25:00+08:00", "status": "FILLED"},
        ],
    })
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["matched_count"] == 0
    assert body["data"]["unmatched_order_ids"] == ["ghost"]


def test_post_trade_result_marks_seeded_order(client, settings_for_test):
    """e2e: 直接 seed 一个 order，再 POST 成交回报，验证状态被标记。"""
    from datetime import datetime, timezone

    from app.db import make_session_factory
    from app.dependencies import _engine_for_url
    from app.models import Order

    engine = _engine_for_url(settings_for_test.db_url)
    sf = make_session_factory(engine)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with sf() as s:
        s.add(Order(order_id="seed-1", account_group="real_A", symbol="A.SH",
                    direction="BUY", quantity=100, limit_price=10.0,
                    valid_date="20260430", status="PENDING", created_at=now))
        s.commit()

    r = client.post("/trade-result", headers=_AUTH, json={
        "trade_date": "20260430",
        "results": [
            {"order_id": "seed-1", "filled_quantity": 100, "filled_price": 10.0,
             "filled_time": "2026-04-30T09:25:00+08:00", "status": "FILLED"},
        ],
    })
    body = r.json()
    assert body["data"]["matched_count"] == 1

    with sf() as s:
        assert s.get(Order, "seed-1").status == "FILLED"
```

---

## 验证 + Commit

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest -v   # 期望 110 + 5 split + 7 service + 2 endpoint = 124
```

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/app/services/settlement.py \
        v2.3/server/app/dependencies.py \
        v2.3/server/app/api/trade_result.py \
        v2.3/server/tests/unit/test_settlement.py \
        v2.3/server/tests/unit/test_api_trade_result.py
git commit -m "feat(server): wire POST /trade-result to SettlementService (Plan 10)"
```

---

## 收尾

- [ ] 124 PASS
- [ ] 1 commit

**🎉 里程碑达成：3 个端点全部真实可用。** server 业务管线打通：
```
client POST /market-data → IngestService → Parquet
client GET /orders        → OrdersQueueService → SQLite
client POST /trade-result → SettlementService → SQLite + 虚拟账本拆分
```

剩下：Plan 11 (perf) / Plan 12 (scheduler 把策略框架挂上去) / Plan 13 (deploy)

---

## 后续 plan

Plan 11: perf NAV（每日净值快照）
