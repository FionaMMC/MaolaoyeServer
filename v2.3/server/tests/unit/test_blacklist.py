"""BlacklistService 单元测试。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import Order
from app.services.blacklist import BlacklistService


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@pytest.fixture
def sf(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    return make_session_factory(engine)


def _add_order(sf, *, symbol, status, valid_date, order_id):
    with sf() as s:
        s.add(Order(
            order_id=order_id, account_group="real_A",
            symbol=symbol, direction="BUY", quantity=100, limit_price=10.0,
            valid_date=valid_date, status=status, created_at=_now(),
        ))
        s.commit()


def test_blacklist_empty_when_no_orders(sf):
    svc = BlacklistService(sf)
    assert svc.compute() == set()


def test_blacklist_skips_non_rejected(sf):
    """只统计 status='REJECTED'，FILLED/PARTIAL/PENDING 不入。"""
    _add_order(sf, symbol="600001.SH", status="FILLED", valid_date="20260507", order_id="a")
    _add_order(sf, symbol="600002.SH", status="PARTIAL", valid_date="20260507", order_id="b")
    _add_order(sf, symbol="600003.SH", status="PENDING", valid_date="20260507", order_id="c")
    _add_order(sf, symbol="600004.SH", status="CANCELLED", valid_date="20260507", order_id="d")

    svc = BlacklistService(sf)
    assert svc.compute() == set()


def test_blacklist_collects_rejected_symbols(sf):
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date="20260507", order_id="a")
    _add_order(sf, symbol="600002.SH", status="REJECTED", valid_date="20260507", order_id="b")

    svc = BlacklistService(sf)
    assert svc.compute() == {"600001.SH", "600002.SH"}


def test_blacklist_dedup_same_symbol(sf):
    """同一 symbol 多次 REJECTED 还是只算一个 entry。"""
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date="20260507", order_id="a")
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date="20260508", order_id="b")
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date="20260509", order_id="c")

    svc = BlacklistService(sf)
    assert svc.compute() == {"600001.SH"}


def test_blacklist_min_rejections_threshold(sf):
    """min_rejections=2 时，只被拒 1 次的不入名单。"""
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date="20260507", order_id="a")
    _add_order(sf, symbol="600002.SH", status="REJECTED", valid_date="20260507", order_id="b")
    _add_order(sf, symbol="600002.SH", status="REJECTED", valid_date="20260508", order_id="c")

    svc = BlacklistService(sf)
    assert svc.compute(min_rejections=2) == {"600002.SH"}


def test_blacklist_lookback_filters_old(sf):
    """超过 lookback_days 的拒单不入名单。"""
    old_date = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
    new_date = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")

    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date=old_date, order_id="a")
    _add_order(sf, symbol="600002.SH", status="REJECTED", valid_date=new_date, order_id="b")

    svc = BlacklistService(sf)
    bl = svc.compute(lookback_days=30)
    assert bl == {"600002.SH"}


def test_blacklist_stats_returns_counts(sf):
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date="20260507", order_id="a")
    _add_order(sf, symbol="600002.SH", status="REJECTED", valid_date="20260507", order_id="b")
    _add_order(sf, symbol="600002.SH", status="REJECTED", valid_date="20260508", order_id="c")

    svc = BlacklistService(sf)
    stats = svc.stats(lookback_days=3650)
    assert stats["rejected_total"] == 3
    assert stats["unique_symbols"] == 2
    assert stats["by_symbol"]["600002.SH"] == 2
    assert stats["by_symbol"]["600001.SH"] == 1
