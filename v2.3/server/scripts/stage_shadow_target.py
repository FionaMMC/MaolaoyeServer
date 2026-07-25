"""Validate and atomically stage a producer target for a configured shadow ledger.

Run as a module from ``v2.3/server`` so the application package is imported
without modifying ``sys.path``:

    python -m scripts.stage_shadow_target --source ... --sidecar ... \
      --shadow-id Shadow_Hydra_V481_RB --trade-date 20260725 --install
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

import pandas as pd

from app.services.shadow_ledger import (
    ShadowLedgerService,
    validate_shadow_sidecar,
)


SERVER_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SERVER_ROOT / "strategies.yaml"


def _read_target(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError("source must be a .parquet or .csv target")


def startup_check(
    source: Path, config_path: Path, shadow_id: str, trade_date: int,
    sidecar: Path | None,
) -> tuple[dict, pd.DataFrame, str]:
    """Fail closed before any destination file is touched."""
    if not source.is_file():
        raise FileNotFoundError(f"source target does not exist: {source}")
    if not config_path.is_file():
        raise FileNotFoundError(f"strategy config does not exist: {config_path}")
    service = ShadowLedgerService(None, None, config_path)
    matches = [cfg for cfg in service.load_instances() if cfg["shadow_id"] == shadow_id]
    if len(matches) != 1:
        raise ValueError(f"expected one configured shadow instance named {shadow_id}")
    cfg = matches[0]
    frame, target_hash = service.validate_target(
        _read_target(source), shadow_id, trade_date, constraints=cfg
    )
    if cfg["require_sidecar"] and sidecar is None:
        raise ValueError(f"{shadow_id} requires a producer provenance sidecar")
    if sidecar is not None:
        if not sidecar.is_file():
            raise FileNotFoundError(f"sidecar does not exist: {sidecar}")
        validate_shadow_sidecar(sidecar, frame, cfg)
    return cfg, frame, target_hash


def _copy_to_temp(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    handle.close()
    try:
        shutil.copy2(source, temp_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def stage_target(
    source: Path, config_path: Path, shadow_id: str, trade_date: int,
    sidecar: Path | None = None, install: bool = False,
) -> dict:
    cfg, frame, target_hash = startup_check(
        source, config_path, shadow_id, trade_date, sidecar
    )
    destination = cfg["target_file"]
    sidecar_destination = destination.with_suffix(".json") if sidecar else None
    if install:
        target_temp = _copy_to_temp(source, destination)
        sidecar_temp = (
            _copy_to_temp(sidecar, sidecar_destination)
            if sidecar is not None and sidecar_destination is not None else None
        )
        try:
            if sidecar_temp is not None and sidecar_destination is not None:
                os.replace(sidecar_temp, sidecar_destination)
            os.replace(target_temp, destination)
        finally:
            target_temp.unlink(missing_ok=True)
            if sidecar_temp is not None:
                sidecar_temp.unlink(missing_ok=True)
    return {
        "shadow_id": shadow_id,
        "validated": True,
        "installed": install,
        "destination": str(destination),
        "rows": len(frame),
        "decision_date": frame["decision_date"].iloc[0],
        "source_version": frame["source_version"].iloc[0],
        "input_hash": frame["input_hash"].iloc[0],
        "target_hash": target_hash,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and optionally atomically install a shadow target."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--sidecar", type=Path)
    parser.add_argument("--shadow-id", required=True)
    parser.add_argument("--trade-date", type=int, required=True, help="YYYYMMDD")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--install", action="store_true",
        help="Install to the configured *_latest path after validation.",
    )
    args = parser.parse_args()
    result = stage_target(
        args.source, args.config, args.shadow_id, args.trade_date,
        sidecar=args.sidecar, install=args.install,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
