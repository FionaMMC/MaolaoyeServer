"""Run the configured no-order shadow ledgers without invoking the order pipeline."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from app.db import init_db, make_engine, make_session_factory
from app.services.shadow_ledger import ShadowLedgerService
from app.settings import Settings, get_settings
from app.storage.parquet import ParquetStore


def startup_check(settings: Settings, trade_date: int) -> None:
    text = str(trade_date)
    if len(text) != 8 or not text.isdigit():
        raise ValueError("trade_date must be YYYYMMDD")
    datetime.strptime(text, "%Y%m%d")
    config = Path(settings.strategies_file)
    if not config.is_file():
        raise FileNotFoundError(f"strategy config does not exist: {config}")
    data_root = Path(settings.parquet_root)
    if not data_root.is_dir():
        raise FileNotFoundError(f"market data root does not exist: {data_root}")


def run_once(settings: Settings, trade_date: int) -> dict:
    startup_check(settings, trade_date)
    engine = make_engine(settings.db_url)
    init_db(engine)
    service = ShadowLedgerService(
        session_factory=make_session_factory(engine),
        parquet_store=ParquetStore(Path(settings.parquet_root)),
        config_path=Path(settings.strategies_file),
    )
    return service.run_all(trade_date)


def exit_code(summary: dict) -> int:
    statuses = {
        str(item.get("status"))
        for item in summary.get("instances", [])
    }
    return 1 if "blocked" in statuses else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--trade-date",
        type=int,
        default=int(datetime.now().strftime("%Y%m%d")),
        help="Ledger mark date in YYYYMMDD form; defaults to today.",
    )
    args = parser.parse_args()
    summary = run_once(get_settings(), args.trade_date)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
