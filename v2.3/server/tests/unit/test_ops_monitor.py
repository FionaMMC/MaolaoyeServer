import json
from pathlib import Path
import pytest
from app.db import init_db, make_engine, make_session_factory
from app.models import PerfSnapshot
from app.services.ops_monitor import OpsMonitorService

@pytest.fixture
def sf(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path}/t.db"); init_db(eng)
    return make_session_factory(eng)

def _snap(s, inst, date, nav, ret, pos):
    s.add(PerfSnapshot(instance_id=inst, date=date, nav=nav, daily_return=ret,
                       positions_snapshot=json.dumps(pos)))

def test_snapshot_integrity_flags_frozen(sf):
    inst = "paper_v53_v53"
    with sf() as s:
        _snap(s, inst, "20260522", 9_951_130, 0.013, {"X": 100})
        _snap(s, inst, "20260525", 9_951_130, 0.0, {"X": 100})  # frozen: same nav, ret 0 on trading day
        s.commit()
    svc = OpsMonitorService(sf)
    issues = svc.snapshot_integrity(inst, lookback=30)["issues"]
    assert any(i["type"] == "frozen" and i["date"] == "20260525" for i in issues)

def test_overnight_position_anomaly_flags_doubling(sf):
    inst = "paper_v53_v53"
    with sf() as s:
        _snap(s, inst, "20260608", 9_888_426, -0.0036, {"511260.SH": 49500, "510300.SH": 118500})
        _snap(s, inst, "20260609", 16_608_072, 0.68, {"511260.SH": 99000, "510300.SH": 118500})
        s.commit()
    svc = OpsMonitorService(sf)
    an = svc.overnight_position_anomalies(inst, threshold=0.5)
    assert any(a["symbol"] == "511260.SH" and round(a["ratio"], 2) == 2.0 for a in an)
    assert all(a["symbol"] != "510300.SH" for a in an)  # unchanged not flagged

def test_pipeline_runs_marks_missing_weekday(sf):
    inst = "paper_v20h_v20h_v1_3"
    from app.models import RawSignal
    with sf() as s:
        s.add(RawSignal(signal_id="a", instance_id=inst, symbol="600000.SH",
                        direction="SELL", quantity=100, reference_price=1.0,
                        price_offset=0.0, limit_price=1.0, valid_date="20260528",
                        signal_time="2026-05-28T16:00:00+08:00", precheck_status="PASS"))
        # 周更策略：0529 这天只标了 NAV 快照（管线确实跑了），但没有调仓信号
        _snap(s, inst, "20260529", 1_000_000, 0.0, {"600000.SH": 100})
        s.commit()
    svc = OpsMonitorService(sf)
    runs = svc.pipeline_runs(lookback_days=4, today="20260601")
    by = {r["valid_date"]: r for r in runs}
    assert by["20260528"]["status"] == "ok"          # 有信号 → 正常
    assert by["20260529"]["status"] == "no_signal"   # 有快照无信号 → 管线跑了但没调仓，非缺失
    assert by["20260601"]["status"] == "missing"     # 既无信号又无快照 → 真缺失

def test_data_freshness_reports_lag(sf, tmp_path):
    import pandas as pd
    from app.storage.parquet import ParquetStore
    store = ParquetStore(root=tmp_path / "data")
    store.append("indexes", "000852.SH", pd.DataFrame([{"trade_date": 20260430, "open":1,"high":1,"low":1,"close":1,"volume":1}]))
    svc = OpsMonitorService(sf, parquet_store=store)
    fr = svc.data_freshness(today="20260608", probe=("indexes", "000852.SH"))
    assert fr["market_latest"] == 20260430
    assert fr["market_lag_days"] == 39


# ── 僵尸挂单 / 孤儿成交（幽灵卖单监控盲区，2026-06 复发）─────────────────
from app.models import Order, Trade


def _order(s, oid, vd, status="PENDING", direction="SELL", symbol="600330.SH", qty=100):
    s.add(Order(order_id=oid, account_group="paper_v20h", symbol=symbol,
                direction=direction, quantity=qty, limit_price=1.0,
                valid_date=vd, status=status,
                created_at=f"{vd[:4]}-{vd[4:6]}-{vd[6:8]}T16:00:00+08:00"))


def _trade(s, oid, received_at, qty=100, status="FILLED"):
    s.add(Trade(order_id=oid, filled_quantity=qty, filled_price=10.0,
                filled_time=received_at, status=status, received_at=received_at))


def test_stale_pending_flags_old_pending_skips_fresh_and_terminal(sf):
    with sf() as s:
        _order(s, "old_pending", "20260622", status="PENDING")     # 4 天前，僵尸
        _order(s, "fresh_pending", "20260626", status="PENDING")   # 今天发的，未到期，不算
        _order(s, "old_filled", "20260622", status="FILLED")       # 已成交，不算
        s.commit()
    svc = OpsMonitorService(sf)
    stale = svc.stale_pending_orders(max_age_days=2, today="20260626")
    ids = {o["order_id"] for o in stale}
    assert ids == {"old_pending"}
    assert stale[0]["age_days"] == 4


def test_orphan_fills_flags_unmatched_recent_skips_matched_and_stale(sf):
    with sf() as s:
        _order(s, "real_order", "20260623", status="PENDING")
        _trade(s, "real_order", "2026-06-24T15:10:01+08:00")            # 有父订单，不算
        _trade(s, "ghost_recent", "2026-06-24T15:10:01+08:00")         # 无父订单 + 近期，孤儿
        _trade(s, "ghost_old", "2026-06-01T15:10:01+08:00")            # 无父订单但超出窗口，不算
        s.commit()
    svc = OpsMonitorService(sf)
    orphans = svc.orphan_fills(lookback_days=7, today="20260626")
    ids = {o["order_id"] for o in orphans}
    assert ids == {"ghost_recent"}
