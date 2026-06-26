"""BlacklistService 单元测试。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import Order
from app.services.blacklist import BlacklistService


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _recent_date(days_ago: int = 3) -> str:
    """A valid_date inside the default 30-day lookback window, anchored to *now*.

    BlacklistService 的 cutoff 是 datetime.now() - lookback，所以测试注入的拒单日期
    必须相对今天、而非写死某个月份——否则随着时间推移会滑出窗口、测试失效。
    """
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d")


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
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date=_recent_date(), order_id="a")
    _add_order(sf, symbol="600002.SH", status="REJECTED", valid_date=_recent_date(), order_id="b")

    svc = BlacklistService(sf)
    assert svc.compute() == {"600001.SH", "600002.SH"}


def test_blacklist_dedup_same_symbol(sf):
    """同一 symbol 多次 REJECTED 还是只算一个 entry。"""
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date=_recent_date(3), order_id="a")
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date=_recent_date(2), order_id="b")
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date=_recent_date(1), order_id="c")

    svc = BlacklistService(sf)
    assert svc.compute() == {"600001.SH"}


def test_blacklist_min_rejections_threshold(sf):
    """min_rejections=2 时，只被拒 1 次的不入名单。"""
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date=_recent_date(3), order_id="a")
    _add_order(sf, symbol="600002.SH", status="REJECTED", valid_date=_recent_date(3), order_id="b")
    _add_order(sf, symbol="600002.SH", status="REJECTED", valid_date=_recent_date(2), order_id="c")

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
    assert stats["auto_rejected_total"] == 3
    assert stats["auto_unique_symbols"] == 2
    assert stats["auto_by_symbol"]["600002.SH"] == 2
    assert stats["auto_by_symbol"]["600001.SH"] == 1
    assert stats["manual_count"] == 0


def test_blacklist_manual_add_and_remove(sf):
    svc = BlacklistService(sf)

    info = svc.add_manual(["600063.SH", "002001.SZ"], reason="delisting")
    assert info == {"added": 2, "updated": 0, "total_input": 2}
    assert svc.compute() == {"600063.SH", "002001.SZ"}

    # 重复 add 是 update
    info2 = svc.add_manual(["600063.SH"], reason="ST")
    assert info2 == {"added": 0, "updated": 1, "total_input": 1}

    assert svc.remove_manual("600063.SH") is True
    assert svc.compute() == {"002001.SZ"}
    assert svc.remove_manual("does_not_exist") is False


def test_blacklist_compute_unions_auto_and_manual(sf):
    """compute() 返回（自动派生 ∪ 手工）的并集。"""
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date=_recent_date(), order_id="a")
    svc = BlacklistService(sf)
    svc.add_manual(["600002.SH"], reason="ST")
    assert svc.compute() == {"600001.SH", "600002.SH"}


def test_blacklist_auto_promote_persists_rejected(sf):
    """auto_promote 把过去 REJECTED 的 symbol 永久存进 risk_blacklist 表。"""
    from app.models import RiskBlacklistEntry

    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date=_recent_date(), order_id="a")
    _add_order(sf, symbol="600002.SH", status="REJECTED", valid_date=_recent_date(), order_id="b")
    _add_order(sf, symbol="600003.SH", status="FILLED", valid_date=_recent_date(), order_id="c")

    svc = BlacklistService(sf)
    info = svc.auto_promote()
    assert info["promoted"] == 2

    # 查持久表：应该有 600001 + 600002，没有 600003（FILLED 的不进）
    with sf() as s:
        from sqlalchemy import select
        rows = s.execute(select(RiskBlacklistEntry.symbol)).all()
        assert {r[0] for r in rows} == {"600001.SH", "600002.SH"}

    # 重复 promote 不重复加
    info2 = svc.auto_promote()
    assert info2["promoted"] == 0
    assert info2["already_present"] == 2


def test_blacklist_auto_promote_survives_clear_state(sf):
    """关键回归：clear-state 把 orders 表清空后，黑名单仍能拿到原 REJECTED symbol。"""
    from sqlalchemy import delete
    _add_order(sf, symbol="600001.SH", status="REJECTED", valid_date=_recent_date(), order_id="a")
    svc = BlacklistService(sf)

    # auto_promote 把 600001 永久化
    svc.auto_promote()
    assert svc.compute() == {"600001.SH"}

    # 模拟 clear-state 清掉 orders 表
    with sf() as s:
        from app.models import Order
        s.execute(delete(Order))
        s.commit()

    # 即便 orders 表空了，compute 还是返回 600001（来自手工/持久部分）
    assert svc.compute() == {"600001.SH"}
