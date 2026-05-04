"""tests/unit/test_db.py"""
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.db import init_db, make_engine, make_session_factory


def test_make_engine_with_sqlite_url(tmp_path: Path):
    db_url = f"sqlite:///{tmp_path}/t.db"
    engine = make_engine(db_url)
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


def test_make_engine_in_memory():
    engine = make_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


def test_init_db_creates_all_tables(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    expected = {
        "instance_state",
        "raw_signals",
        "orders",
        "order_signal_map",
        "trades",
        "perf_snapshots",
    }
    assert expected.issubset(tables), f"缺表: {expected - tables}"


def test_init_db_is_idempotent(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    init_db(engine)
    inspector = inspect(engine)
    assert "orders" in inspector.get_table_names()


def test_session_factory_yields_working_session(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    SessionFactory = make_session_factory(engine)
    with SessionFactory() as s:
        result = s.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        names = {r[0] for r in result.fetchall()}
        assert "orders" in names
