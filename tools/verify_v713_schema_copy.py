"""Verify additive schema compatibility on a remote SQLite backup, never the source.

Run on qmt. Keep the source and resulting copy on that host; export only the
JSON receipt. This script does not start the app, scheduler or broker client.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3


def verify(source: Path, output: Path) -> dict:
    source, output = source.resolve(strict=True), output.resolve()
    if output.exists() or source == output or output.parent == source.parent:
        raise ValueError("use a new copy in a separate isolated working directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    original = sqlite3.connect(source.as_uri()+"?mode=ro", uri=True)
    original.execute("PRAGMA query_only=ON")
    copy = sqlite3.connect(output)
    try:
        original.backup(copy)
    finally:
        original.close()
    def schema():
        return dict(copy.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"))
    before = schema()
    tables = [r[0] for r in copy.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    def counts():
        return {name: copy.execute('SELECT count(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for name in tables}
    prior_counts = counts()
    from app.db import init_db, make_engine
    engine = make_engine("sqlite:///"+str(output))
    init_db(engine)
    engine.dispose()
    after = schema()
    assert all(after.get(name) == sql for name, sql in before.items()), "existing schema changed"
    assert counts() == prior_counts, "existing data changed"
    assert copy.execute("PRAGMA quick_check").fetchall() == [("ok",)], "copy integrity failed"
    added = sorted(set(after)-set(before))
    assert "accounting_receipts" in after, "accounting receipt table missing"
    # Inspect all mapped columns without reading any business rows into output.
    from app.models import Base
    for table in Base.metadata.sorted_tables:
        copy.execute('SELECT '+','.join('"'+c.name+'"' for c in table.columns)+' FROM "'+table.name+'" LIMIT 0')
    copy.close()
    return {"passed": True, "source_open_mode": "ro+query_only", "schema_applied_to": "isolated_backup_only",
            "existing_schema_unchanged": True, "existing_table_counts_unchanged": True,
            "integrity": "ok", "mapped_tables_checked": len(Base.metadata.tables),
            "added_schema_objects": added, "production_services_changed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--copy-db", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    receipt = verify(args.source_db, args.copy_db)
    args.receipt.write_text(json.dumps(receipt, indent=2)+"\n")
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
