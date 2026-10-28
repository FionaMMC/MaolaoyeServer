"""Value the existing V713 stock leg; database writes are restricted to derived performance tables."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.models import InstanceState
from app.services.live_performance import materialize_live_performance
from app.storage.parquet import ParquetStore

INSTANCE = "live_v713_stockleg"
PERFORMANCE_TABLES = frozenset({"perf_snapshots", "perf_valuations", "daily_risk_snapshots"})


def performance_sessions(db_url):
    """An existing SQLite DB, with a connection-level write allowlist and no schema creation."""
    url = make_url(db_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("stock valuation requires an existing file-backed SQLite database")
    path = Path(url.database).resolve(strict=True)

    def authorize(action, table, column, database, trigger):
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            return sqlite3.SQLITE_OK if table in PERFORMANCE_TABLES else sqlite3.SQLITE_DENY
        if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_ALTER_TABLE}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def connect():
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
        connection.set_authorizer(authorize)
        return connection

    engine = create_engine("sqlite://", creator=connect)
    return engine, sessionmaker(engine, expire_on_commit=False)


def value_stock(sf, store, evidence_path, *, now=None):
    with sf() as session:
        state = session.get(InstanceState, INSTANCE)
        if state is None:
            return {"status": "WAITING_INSTANCE", "written": 0, "instance_id": INSTANCE}
        if state.execution_domain != "live" or state.ledger_mode != "attributed":
            return {"status": "WAITING_INSTANCE_CONFIGURATION", "written": 0, "instance_id": INSTANCE}
        held = sorted(symbol for symbol, quantity in (state.virtual_positions or {}).items() if quantity)
        if not held and float(state.virtual_cash) <= 0:
            return {"status": "WAITING_CAPITAL", "written": 0, "instance_id": INSTANCE}
    evidence = {}
    if held:
        try:
            payload = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
            evidence = payload.get(INSTANCE, {}) if isinstance(payload, dict) else {}
        except (OSError, ValueError, TypeError):
            evidence = {}
        missing = [symbol for symbol in held if not isinstance(evidence, dict)
                   or not isinstance(evidence.get(symbol), dict) or not evidence[symbol]]
        if missing:
            # Do not silently use stale strategy metadata when the configured
            # evidence file is absent or incomplete. Existing validation checks
            # adjustment, source and the full file hash for every provided symbol.
            return {"status": "WAITING_RAW_PRICE_EVIDENCE", "written": 0, "symbols": missing}
    # Keep an explicit file-evidence scope even for a cash-only observation. If
    # holdings appear concurrently, the valuation must wait for their proofs,
    # rather than falling back to strategy metadata through an empty dictionary.
    evidence = evidence or {"__explicit_file_evidence__": {}}
    return materialize_live_performance(sf, store, INSTANCE, now=now, raw_price_evidence=evidence)


def main(argv=None, *, now=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-price-evidence", type=Path, required=True,
                        help="JSON mapping live_v713_stockleg -> symbol -> verified raw-price file evidence")
    args = parser.parse_args(argv)
    from app.settings import get_settings
    settings = get_settings()
    engine, sf = performance_sessions(settings.db_url)
    try:
        result = value_stock(sf, ParquetStore(settings.parquet_root), args.raw_price_evidence, now=now)
    finally:
        engine.dispose()
    print(json.dumps({**result, "instance_id": INSTANCE, "allowed_write_tables": sorted(PERFORMANCE_TABLES)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
