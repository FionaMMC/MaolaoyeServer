"""The strategy-capital schema upgrade must work on an existing SQLite DB."""
from __future__ import annotations

import sqlite3

from scripts.migrate_db import main


def test_migration_adds_strategy_ledger_columns_idempotently(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("""
            CREATE TABLE instance_state (
                instance_id VARCHAR PRIMARY KEY,
                execution_domain VARCHAR NOT NULL DEFAULT 'paper',
                account_alias VARCHAR,
                virtual_cash FLOAT NOT NULL,
                virtual_positions JSON NOT NULL,
                last_update VARCHAR NOT NULL,
                strategy_state JSON,
                owned_symbols JSON
            )
        """)
        connection.execute("""
            CREATE TABLE cash_flow_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_domain VARCHAR NOT NULL,
                account_alias VARCHAR NOT NULL,
                instance_id VARCHAR NOT NULL,
                event_date VARCHAR NOT NULL,
                event_type VARCHAR NOT NULL,
                amount FLOAT NOT NULL,
                currency VARCHAR NOT NULL,
                source VARCHAR NOT NULL,
                source_event_id VARCHAR NOT NULL,
                evidence_sha256 VARCHAR NOT NULL,
                description VARCHAR,
                status VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                applied_at VARCHAR NOT NULL
            )
        """)

    args = [
        "--db-url", f"sqlite:///{db_path}", "--skip-stale-cleanup",
    ]
    assert main(args) == 0
    assert main(args) == 0

    with sqlite3.connect(db_path) as connection:
        state_columns = {
            row[1]: row for row in connection.execute(
                "PRAGMA table_info(instance_state)"
            )
        }
        flow_columns = {
            row[1]: row for row in connection.execute(
                "PRAGMA table_info(cash_flow_journal)"
            )
        }
    assert state_columns["ledger_mode"][4] == "'legacy'"
    assert {
        "qmt_cash_snapshot", "snapshot_time", "transition_to_attributed",
    }.issubset(flow_columns)
