"""Validate and install a no-order shadow target snapshot.

The script only stages a parquet plus its sidecar under plugins/v713/shadow_targets.
It does not create signals, orders, or calls to QMT.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app.shadow.target_staging import MAX_TARGET_AGE_DAYS, stage_target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", required=True, type=Path)
    parser.add_argument("--sidecar", required=True, type=Path)
    parser.add_argument("--target-dir", type=Path, default=SERVER_ROOT / "plugins" / "v713" / "shadow_targets")
    parser.add_argument("--as-at", type=date.fromisoformat, default=date.today())
    parser.add_argument("--max-age-days", type=int, default=MAX_TARGET_AGE_DAYS)
    args = parser.parse_args()
    staged = stage_target(args.parquet, args.sidecar, args.target_dir, as_at=args.as_at, max_age_days=args.max_age_days)
    print(f"staged {staged.shadow_id}: rows={staged.rows} decision_date={staged.decision_date} input_hash={staged.input_hash}")


if __name__ == "__main__":
    main()
