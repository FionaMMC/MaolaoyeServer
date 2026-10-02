"""Run the existing QMT collector as an isolated Hydra live diagnostic backup."""
from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path


def _code_provenance(code_dir: Path, configured_commit: str = "") -> dict:
    """Record release provenance without making diagnostics depend on a repo pin.

    An unrelated commit must not disable the collector. Its actual source files
    still need to be committed, and the upload must return its normal receipt.
    """
    actual = subprocess.run(
        ["git", "-C", str(code_dir), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(code_dir), "status", "--porcelain", "--", "client/market_push.py", "config.py"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError("market backup source files have uncommitted changes")
    configured_valid = len(configured_commit) == 40 and all(
        char in "0123456789abcdef" for char in configured_commit
    )
    return {
        "event": "MARKET_BACKUP_CODE_PROVENANCE",
        "actual_commit": actual,
        "configured_commit": configured_commit if configured_valid else None,
        "matches_configured_commit": actual == configured_commit if configured_valid else None,
        "collector_sources_clean": True,
    }


def main() -> None:
    code_dir = Path(os.environ["HYDRA_LIVE_CODE_DIR"])
    provenance = _code_provenance(code_dir, os.environ.get("HYDRA_LIVE_CODE_COMMIT", ""))
    client_script = code_dir / "client" / "market_push.py"
    if not client_script.is_file():
        raise RuntimeError(f"market_push.py not found under HYDRA_LIVE_CODE_DIR: {code_dir}")
    print(json.dumps(provenance, sort_keys=True))
    live_userdata = os.environ["HYDRA_LIVE_QMT_USERDATA_DIR"]
    backup_key = os.environ["HYDRA_LIVE_DATA_BACKUP_API_KEY"]
    webhook = os.environ.get("HYDRA_LIVE_WECHAT_WEBHOOK", "")

    # Existing market_push.py reads config at import time. These values keep its
    # collector local while --live-backup writes only the isolated backup API.
    os.environ["QMT_PIPELINE_PUSH_MODE"] = "local"
    os.environ["QMT_PIPELINE_WECOM_WEBHOOK"] = webhook
    os.environ["HYDRA_LIVE_DATA_BACKUP_API_KEY"] = backup_key
    sys.path.insert(0, str(code_dir))
    import config
    config.QMT_USERDATA_DIR = live_userdata
    config.PUSH_MODE = "local"
    config.WECOM_WEBHOOK_URL = webhook

    sys.argv = [str(client_script), "--live-backup"]
    runpy.run_path(str(client_script), run_name="__main__")


if __name__ == "__main__":
    main()
