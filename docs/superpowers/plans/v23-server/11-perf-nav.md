# Plan 11: 策略绩效 NAV 快照

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 每个策略实例每个交易日生成一条净值快照：`nav = virtual_cash + Σ(persisted_position × close_price)`。

**Architecture:** `PerfService.snapshot_all(trade_date)` —— 遍历所有 InstanceState，对每个 instance 用 ParquetStore 查每个持仓标的当日 close，算出 NAV 写入 perf_snapshots 表。可选计算 daily_return（与昨日对比）。

**Files:**
- `v2.3/server/app/services/perf.py` (NEW)
- `v2.3/server/app/dependencies.py` (MODIFY，加 perf service factory)
- `v2.3/server/tests/unit/test_perf.py` (NEW)

---

## Task 1: PerfService + 单测

### `app/services/perf.py`

```python
"""策略实例每日 NAV 快照。"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.models import InstanceState, PerfSnapshot
from app.storage.parquet import ParquetStore

logger = logging.getLogger(__name__)


class PerfService:
    """计算并存储策略实例的每日净值快照。"""

    def __init__(self, session_factory, parquet_store: ParquetStore):
        self.session_factory = session_factory
        self.store = parquet_store

    def snapshot_all(self, trade_date: int) -> int:
        """对所有 instance 生成当日快照。返回写入条数。

        nav = virtual_cash + Σ(position[symbol] × close_price[symbol])

        若某只持仓股票当日无 close 数据（停牌或新上市），用最近一条 close 兜底；
        仍无数据则跳过该持仓的市值（仅算现金部分），并记 warning。
        """
        date_str = str(trade_date)
        with self.session_factory() as session:
            instances = session.execute(select(InstanceState)).scalars().all()
            written = 0
            for inst in instances:
                nav = self._compute_nav(inst, trade_date)
                positions_json = dict(inst.virtual_positions or {})

                # upsert：先查再决定 add 或更新
                existing = session.get(
                    PerfSnapshot, (inst.instance_id, date_str)
                )
                daily_return = self._compute_daily_return(
                    session, inst.instance_id, date_str, nav,
                )
                if existing:
                    existing.nav = nav
                    existing.daily_return = daily_return
                    existing.positions_snapshot = positions_json
                else:
                    session.add(PerfSnapshot(
                        instance_id=inst.instance_id,
                        date=date_str,
                        nav=nav,
                        daily_return=daily_return,
                        positions_snapshot=positions_json,
                    ))
                written += 1
            session.commit()
        return written

    # ── 内部 ──────────────────────────────────────────────────────────
    def _compute_nav(self, inst: InstanceState, trade_date: int) -> float:
        nav = float(inst.virtual_cash)
        positions = inst.virtual_positions or {}
        for symbol, qty in positions.items():
            close = self._latest_close_on_or_before(symbol, trade_date)
            if close is None:
                logger.warning(
                    "instance %s 持仓 %s 在 %s 及之前无 close 数据，市值按 0 计算",
                    inst.instance_id, symbol, trade_date,
                )
                continue
            nav += qty * close
        return round(nav, 4)

    def _latest_close_on_or_before(self, symbol: str, trade_date: int) -> float | None:
        # 先尝试 stocks，再 etfs（兼容 ETF 持仓）
        for category in ("stocks", "etfs"):
            df = self.store.read(category, symbol, end_date=trade_date)
            if not df.empty:
                return float(df["close"].iloc[-1])
        return None

    def _compute_daily_return(
        self, session, instance_id: str, date_str: str, today_nav: float,
    ) -> float | None:
        """跟昨日（数据库里上一条快照）算日收益率。无昨日返回 None。"""
        prev = session.execute(
            select(PerfSnapshot)
            .where(PerfSnapshot.instance_id == instance_id)
            .where(PerfSnapshot.date < date_str)
            .order_by(PerfSnapshot.date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if prev is None or prev.nav == 0:
            return None
        return round((today_nav - prev.nav) / prev.nav, 6)
```

### `tests/unit/test_perf.py`

```python
"""PerfService 单元测试"""
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState, PerfSnapshot
from app.services.perf import PerfService
from app.storage.parquet import ParquetStore


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _setup(tmp_path: Path) -> tuple:
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    sf = make_session_factory(engine)
    store = ParquetStore(root=tmp_path / "parquet")
    return sf, store


def _bar(d: int, close: float) -> dict:
    return {"trade_date": d, "open": close, "high": close, "low": close,
            "close": close, "volume": 1000, "amount": close * 1000,
            "suspendFlag": 0}


def test_snapshot_pure_cash(tmp_path: Path):
    sf, store = _setup(tmp_path)
    with sf() as s:
        s.add(InstanceState(instance_id="i1", virtual_cash=500_000.0,
                            virtual_positions={}, last_update=_now()))
        s.commit()

    svc = PerfService(session_factory=sf, parquet_store=store)
    n = svc.snapshot_all(20260430)

    assert n == 1
    with sf() as s:
        snap = s.get(PerfSnapshot, ("i1", "20260430"))
        assert snap.nav == 500_000.0
        assert snap.daily_return is None   # 无昨日


def test_snapshot_cash_plus_positions(tmp_path: Path):
    sf, store = _setup(tmp_path)
    store.append("stocks", "600519.SH", pd.DataFrame([_bar(20260430, 100.0)]))
    with sf() as s:
        s.add(InstanceState(
            instance_id="i1", virtual_cash=500_000.0,
            virtual_positions={"600519.SH": 200},
            last_update=_now(),
        ))
        s.commit()

    svc = PerfService(session_factory=sf, parquet_store=store)
    svc.snapshot_all(20260430)

    with sf() as s:
        snap = s.get(PerfSnapshot, ("i1", "20260430"))
        # nav = 500_000 + 200 * 100 = 520_000
        assert snap.nav == 520_000.0


def test_snapshot_uses_latest_close_when_today_missing(tmp_path: Path):
    """持仓股票今日没数据时，用最近一日 close 兜底。"""
    sf, store = _setup(tmp_path)
    store.append("stocks", "A.SH", pd.DataFrame([_bar(20260428, 50.0)]))
    # 注意：20260430 没数据
    with sf() as s:
        s.add(InstanceState(instance_id="i1", virtual_cash=0.0,
                            virtual_positions={"A.SH": 100},
                            last_update=_now()))
        s.commit()

    svc = PerfService(session_factory=sf, parquet_store=store)
    svc.snapshot_all(20260430)

    with sf() as s:
        snap = s.get(PerfSnapshot, ("i1", "20260430"))
        assert snap.nav == 5000.0   # 100 * 50（用 04-28 的 close）


def test_snapshot_falls_back_to_etfs_category(tmp_path: Path):
    """持仓在 etfs 表里时也能取到 close。"""
    sf, store = _setup(tmp_path)
    store.append("etfs", "510300.SH", pd.DataFrame([_bar(20260430, 4.0)]))
    with sf() as s:
        s.add(InstanceState(instance_id="i1", virtual_cash=0.0,
                            virtual_positions={"510300.SH": 1000},
                            last_update=_now()))
        s.commit()

    svc = PerfService(session_factory=sf, parquet_store=store)
    svc.snapshot_all(20260430)

    with sf() as s:
        snap = s.get(PerfSnapshot, ("i1", "20260430"))
        assert snap.nav == 4000.0


def test_snapshot_unknown_position_treated_as_zero(tmp_path: Path):
    """完全没数据的持仓按 0 市值。"""
    sf, store = _setup(tmp_path)
    with sf() as s:
        s.add(InstanceState(instance_id="i1", virtual_cash=1000.0,
                            virtual_positions={"GHOST.SH": 200},
                            last_update=_now()))
        s.commit()

    svc = PerfService(session_factory=sf, parquet_store=store)
    svc.snapshot_all(20260430)

    with sf() as s:
        snap = s.get(PerfSnapshot, ("i1", "20260430"))
        assert snap.nav == 1000.0   # 仅现金


def test_snapshot_daily_return_uses_yesterday(tmp_path: Path):
    sf, store = _setup(tmp_path)
    with sf() as s:
        s.add(InstanceState(instance_id="i1", virtual_cash=110_000.0,
                            virtual_positions={}, last_update=_now()))
        s.add(PerfSnapshot(instance_id="i1", date="20260429",
                           nav=100_000.0, daily_return=None,
                           positions_snapshot={}))
        s.commit()

    svc = PerfService(session_factory=sf, parquet_store=store)
    svc.snapshot_all(20260430)

    with sf() as s:
        snap = s.get(PerfSnapshot, ("i1", "20260430"))
        # daily_return = (110_000 - 100_000) / 100_000 = 0.1
        assert snap.daily_return == 0.1


def test_snapshot_idempotent_overwrites(tmp_path: Path):
    """同一日同一 instance 重复 snapshot：覆盖。"""
    sf, store = _setup(tmp_path)
    with sf() as s:
        s.add(InstanceState(instance_id="i1", virtual_cash=100.0,
                            virtual_positions={}, last_update=_now()))
        s.commit()

    svc = PerfService(session_factory=sf, parquet_store=store)
    svc.snapshot_all(20260430)
    # 改 cash 再 snapshot
    with sf() as s:
        s.get(InstanceState, "i1").virtual_cash = 200.0
        s.commit()
    svc.snapshot_all(20260430)

    with sf() as s:
        snap = s.get(PerfSnapshot, ("i1", "20260430"))
        assert snap.nav == 200.0
        # 还是只有 1 条
        cnt = s.query(PerfSnapshot).filter_by(instance_id="i1",
                                              date="20260430").count()
        assert cnt == 1


def test_snapshot_no_instances_returns_zero(tmp_path: Path):
    sf, store = _setup(tmp_path)
    svc = PerfService(session_factory=sf, parquet_store=store)
    assert svc.snapshot_all(20260430) == 0
```

---

## Task 2: 接入 Depends

### `app/dependencies.py` —— 末尾追加

```python
from app.services.perf import PerfService


def get_perf_service(
    sf: sessionmaker = Depends(get_session_factory),
    store: ParquetStore = Depends(get_parquet_store),
) -> PerfService:
    return PerfService(session_factory=sf, parquet_store=store)
```

(暂不加 HTTP endpoint —— 绩效快照由 scheduler 在收盘后触发，不对外暴露 GET。后续如需查询 NAV 历史，再加 endpoint。)

---

## 验证 + Commit

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest -v   # 期望 125 + 8 = 133 PASS
```

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/app/services/perf.py \
        v2.3/server/app/dependencies.py \
        v2.3/server/tests/unit/test_perf.py
git commit -m "feat(server): add PerfService for daily NAV snapshots (Plan 11)"
```

---

## 收尾

- [ ] 133 PASS
- [ ] 1 commit

---

## 后续 plan

Plan 12: scheduler 编排（APScheduler 把 strategy framework + precheck + aggregate + orders queue + perf 串起来；行情入库后自动触发）
