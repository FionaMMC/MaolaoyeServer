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
