from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import select

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState, Order, PerfSnapshot, PerfValuation, DailyRiskSnapshot
from app.storage.parquet import ParquetStore
from app.services.live_performance import materialize_live_performance


@pytest.fixture
def setup(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/test.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as session:
        session.add(InstanceState(instance_id="live_test", execution_domain="live", account_alias="scoped",
            ledger_mode="attributed", virtual_cash=1000, virtual_positions={"511260.SH":100},
            last_update="2026-09-14T10:00:00+08:00"))
        session.commit()
    store = ParquetStore(tmp_path)
    store.append("indexes", "000852.SH", pd.DataFrame({
        "trade_date":[20260911,20260914,20260915,20260916], "close":[100,101,102,103]}))
    store.append("etfs", "511260.SH", pd.DataFrame({
        "trade_date":[20260911,20260914,20260915,20260916], "close":[90,100,101,102]}))
    yield sf, store
    engine.dispose()


def test_live_backfill_starts_at_observed_ledger_and_never_writes_orders(setup):
    sf, store = setup
    now = datetime(2026,9,16,18,tzinfo=ZoneInfo("Asia/Shanghai"))
    first = materialize_live_performance(sf, store, "live_test", now=now, backfill=True)
    assert first["dates"] == ["20260914","20260915","20260916"]
    materialize_live_performance(sf, store, "live_test", now=now, backfill=True)
    with sf() as session:
        rows = session.scalars(select(PerfSnapshot).order_by(PerfSnapshot.date)).all()
        assert [r.nav for r in rows] == [11000,11100,11200]
        assert rows[0].daily_return is None
        assert rows[1].daily_return == pytest.approx(100/11000, abs=1e-6)
        assert session.query(PerfValuation).count() == session.query(DailyRiskSnapshot).count() == 3
        assert session.query(Order).count() == 0
        state = session.get(InstanceState, "live_test")
        assert state.virtual_cash == 1000 and state.last_update == "2026-09-14T10:00:00+08:00"


def test_does_not_label_intraday_or_stale_prices_as_eod(setup):
    sf, store = setup
    now = datetime(2026,9,16,10,tzinfo=ZoneInfo("Asia/Shanghai"))
    assert materialize_live_performance(sf,store,"live_test",now=now)["dates"] == ["20260915"]
    path = store._file("etfs", "511260.SH")
    data = pd.read_parquet(path)
    data.loc[data.trade_date == 20260916,"close"] = float('nan')
    data.to_parquet(path,index=False)
    now = now.replace(hour=18)
    result = materialize_live_performance(sf,store,"live_test",now=now)
    assert result["written"] == 0 and result["missing_price_dates"] == ["20260916"]
    with sf() as session:
        assert session.get(PerfSnapshot,("live_test","20260916")) is None


def test_refuses_paper_ledger(setup):
    sf,store = setup
    with sf() as session:
        session.get(InstanceState,"live_test").execution_domain = "paper"
        session.commit()
    with pytest.raises(ValueError,match="attributed live"):
        materialize_live_performance(sf,store,"live_test",now=datetime(2026,9,16,18,tzinfo=ZoneInfo("Asia/Shanghai")))


def test_market_file_reads_do_not_hold_sqlite_write_lock_and_changed_ledger_retries(setup,monkeypatch):
    from app.services.ledger_transaction import begin_ledger_transaction
    sf,store = setup
    original = store.read
    changed = False
    def read(*args,**kwargs):
        nonlocal changed
        if args[0] == 'etfs' and not changed:
            with sf() as session:
                begin_ledger_transaction(session,'live','scoped')
                session.get(InstanceState,'live_test').virtual_cash += 500
                session.commit()
            changed = True
        return original(*args,**kwargs)
    monkeypatch.setattr(store,'read',read)
    result = materialize_live_performance(sf,store,'live_test',
        now=datetime(2026,9,16,18,tzinfo=ZoneInfo('Asia/Shanghai')),backfill=True)
    assert result['status'] == 'LEDGER_CHANGED_RETRY'
    with sf() as session:
        assert session.query(PerfSnapshot).count() == 0
        assert session.get(InstanceState,'live_test').virtual_cash == 1500
