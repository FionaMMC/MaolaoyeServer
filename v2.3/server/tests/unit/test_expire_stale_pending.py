import pytest
from app.db import init_db, make_engine, make_session_factory
from app.models import Order, Trade
from scripts.expire_stale_pending import expire_stale_pending


@pytest.fixture
def sf(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(eng)
    return make_session_factory(eng)


def _order(s, oid, vd, status="PENDING"):
    s.add(Order(order_id=oid, account_group="paper_v20h", symbol="600330.SH",
                direction="SELL", quantity=100, limit_price=1.0, valid_date=vd,
                status=status, created_at=f"{vd}T16:00:00+08:00"))


def test_dry_run_selects_but_does_not_mutate(sf):
    with sf() as s:
        _order(s, "zombie", "20260622")
        s.commit()
    res = expire_stale_pending(sf, max_age_days=2, today="20260626", apply=False)
    assert res["candidates"] == 1
    assert res["to_expire"] == 1
    assert res["expired"] == 0           # dry run never mutates
    with sf() as s:
        assert s.get(Order, "zombie").status == "PENDING"


def test_apply_expires_only_stale_pending(sf):
    with sf() as s:
        _order(s, "zombie", "20260622", "PENDING")    # stale → EXPIRED
        _order(s, "fresh", "20260626", "PENDING")     # today,未到期 → keep
        _order(s, "done", "20260622", "FILLED")       # terminal → keep
        s.commit()
    res = expire_stale_pending(sf, max_age_days=2, today="20260626", apply=True)
    assert res["expired"] == 1
    with sf() as s:
        assert s.get(Order, "zombie").status == "EXPIRED"
        assert s.get(Order, "fresh").status == "PENDING"
        assert s.get(Order, "done").status == "FILLED"


def test_apply_skips_pending_with_matching_fill(sf):
    """防御：万一某 PENDING 单真有匹配成交，不可误判 EXPIRED，留人工核对。"""
    with sf() as s:
        _order(s, "filled_but_pending", "20260622", "PENDING")
        s.add(Trade(order_id="filled_but_pending", filled_quantity=100, filled_price=10.0,
                    filled_time="t", status="FILLED", received_at="2026-06-23T15:10:01+08:00"))
        s.commit()
    res = expire_stale_pending(sf, max_age_days=2, today="20260626", apply=True)
    assert res["expired"] == 0
    assert res["skipped_filled"] == ["filled_but_pending"]
    with sf() as s:
        assert s.get(Order, "filled_but_pending").status == "PENDING"


def test_account_group_limits_expiration_scope(sf):
    with sf() as s:
        _order(s, "v20h-old", "20260622", "PENDING")
        s.add(Order(
            order_id="v79-old", account_group="paper_v79", symbol="512400.SH",
            direction="BUY", quantity=100, limit_price=1.0,
            valid_date="20260622", status="PENDING",
            created_at="20260622T16:00:00+08:00",
        ))
        s.commit()

    res = expire_stale_pending(
        sf, max_age_days=2, today="20260626", apply=True,
        account_group="paper_v79",
    )
    assert res["expired"] == 1
    with sf() as s:
        assert s.get(Order, "v79-old").status == "EXPIRED"
        assert s.get(Order, "v20h-old").status == "PENDING"
