import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.db import init_db, make_engine, make_session_factory
from app.oms.models import OmsFill, OmsSession

OMS_TABLES = {"oms_target_versions", "oms_cycles", "oms_sessions", "oms_orders", "oms_order_events",
              "oms_fills", "oms_broker_snapshots", "oms_reconciliations"}


def _sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/oms.db")
    init_db(engine)
    return engine, make_session_factory(engine)


def test_init_db_creates_all_oms_tables(tmp_path):
    engine, _ = _sf(tmp_path)
    assert OMS_TABLES <= set(inspect(engine).get_table_names())


def _fill(trade_id):
    return OmsFill(account_alias="hydra-live", broker_trade_id=trade_id, symbol="510300.SH", side="BUY",
                   quantity=100, price=4.6, traded_at="2026-10-09T09:25:00+08:00", received_at="x")


def test_broker_trade_id_is_unique_per_account(tmp_path):
    _, sf = _sf(tmp_path)
    with sf() as s:
        s.add(_fill("t1"))
        s.add(_fill("t1"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_session_seq_is_unique_per_cycle(tmp_path):
    _, sf = _sf(tmp_path)
    with sf() as s:
        for sid in ("C00001:1", "C00001:1b"):
            s.add(OmsSession(session_id=sid, cycle_id="C00001", seq=1, trade_date="20261008", phase="SELL",
                             status="PLANNED", deferrals=[], created_at="x"))
        with pytest.raises(IntegrityError):
            s.commit()
