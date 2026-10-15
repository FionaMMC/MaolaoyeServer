"""Repeat the synthetic server/agent/broker handoff gate in an isolated directory.

No production configuration, market downloads or QMT connection are used. The
source package must be on PYTHONPATH (v2.3/server and v2.3).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile


def run(output: Path) -> dict:
    from tests.integration.test_v713_shared_rehearsal import (
        test_stock_rotation_shared_etf_and_full_window_match_broker,
        test_lost_submit_response_restart_does_not_duplicate_stock_sale,
    )
    class Patch:
        def __init__(self):
            self.originals = []
        def setattr(self, obj, key, value):
            self.originals.append((obj, key, getattr(obj, key)))
            setattr(obj, key, value)
        def restore(self):
            for obj, key, value in reversed(self.originals):
                setattr(obj, key, value)
    scenarios = [test_stock_rotation_shared_etf_and_full_window_match_broker,
                 test_lost_submit_response_restart_does_not_duplicate_stock_sale]
    checks = []
    for scenario in scenarios:
        patch = Patch()
        try:
            with tempfile.TemporaryDirectory(prefix="v713-shared-rehearsal-") as directory:
                scenario(Path(directory), patch)
            checks.append({"scenario": scenario.__name__, "passed": True})
        finally:
            patch.restore()
    receipt = {"passed": True, "broker": "synthetic", "production_database_accessed": False,
               "production_services_changed": False, "orders_submitted_to_real_broker": 0, "checks": checks}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2)+"\n")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
