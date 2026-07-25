"""Validate and atomically install no-order shadow portfolio targets.

This module deliberately has no dependency on the strategy runner, order queue,
or QMT client.  A staged target is input for a virtual ledger only; it cannot
become an order through this interface.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd


SERVER_COLUMNS = [
    "shadow_id", "code", "weight", "decision_date", "as_of_date",
    "state_reason", "source_version", "input_hash",
]
SHADOW_IDS = {
    "Shadow_Base",
    "Shadow_Aux_Hard_TOP2",
    "Shadow_ML_TOP2",
    "Shadow_Hydra_V481_RB",
}
CODE_RE = re.compile(r"^\d{6}\.(SH|SZ)$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_VERSION_RE = {
    "Shadow_Base": re.compile(r"^[0-9a-f]{40}$"),
    "Shadow_Aux_Hard_TOP2": re.compile(r"^v7\.9-hard-logistic-aux-top2-r1@[0-9a-f]{40}$"),
    "Shadow_ML_TOP2": re.compile(r"^sw2021-aux-top2-logistic-[^@]+@[0-9a-f]{64}:[0-9a-f]{40}$"),
    "Shadow_Hydra_V481_RB": re.compile(r"^v48\.1-RB@[0-9a-f]{40}$"),
}
MAX_TARGET_AGE_DAYS = 40


@dataclass(frozen=True)
class StagedTarget:
    shadow_id: str
    parquet_path: Path
    sidecar_path: Path
    rows: int
    decision_date: str
    input_hash: str


def _parse_yyyymmdd(value: object, field: str) -> date:
    text = str(value)
    if not re.fullmatch(r"\d{8}", text):
        raise ValueError(f"{field} must be YYYYMMDD")
    return date(int(text[:4]), int(text[4:6]), int(text[6:]))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _one_value(frame: pd.DataFrame, column: str) -> str:
    values = frame[column].astype(str).unique()
    if len(values) != 1:
        raise ValueError(f"{column} must have exactly one value")
    return values[0]


def _validate_frame(frame: pd.DataFrame, as_at: date, max_age_days: int) -> tuple[str, str, str]:
    if list(frame.columns) != SERVER_COLUMNS:
        raise ValueError(f"target columns must be exactly {SERVER_COLUMNS}")
    if frame.empty or frame.isna().any().any():
        raise ValueError("target cannot be empty or contain nulls")
    shadow_id = _one_value(frame, "shadow_id")
    if shadow_id not in SHADOW_IDS:
        raise ValueError(f"unsupported shadow_id: {shadow_id}")
    if not frame["code"].astype(str).map(CODE_RE.fullmatch).all():
        raise ValueError("target contains a non-tradeable code")
    weights = pd.to_numeric(frame["weight"], errors="raise")
    if not (weights > 0).all() or abs(float(weights.sum()) - 1.0) > 1e-8:
        raise ValueError("target weights must be positive and sum to one")
    decision = _one_value(frame, "decision_date")
    as_of = _one_value(frame, "as_of_date")
    decision_day = _parse_yyyymmdd(decision, "decision_date")
    as_of_day = _parse_yyyymmdd(as_of, "as_of_date")
    if as_of_day > decision_day:
        raise ValueError("as_of_date cannot be after decision_date")
    if decision_day > as_at:
        raise ValueError("target decision_date cannot be in the future")
    if (as_at - decision_day).days > max_age_days:
        raise ValueError(f"target is older than {max_age_days} days")
    source_version = _one_value(frame, "source_version")
    if not SOURCE_VERSION_RE[shadow_id].fullmatch(source_version):
        raise ValueError(f"source_version is not allowed for {shadow_id}")
    input_hash = _one_value(frame, "input_hash")
    if not HASH_RE.fullmatch(input_hash):
        raise ValueError("input_hash must be a lowercase SHA256")
    return shadow_id, decision, input_hash


def _validate_sidecar(frame: pd.DataFrame, sidecar: dict) -> None:
    required = {
        "shadow_id", "source_version", "input_hash", "decision_date",
        "as_of_date", "weight_sum", "state_reason",
    }
    missing = required - set(sidecar)
    if missing:
        raise ValueError(f"sidecar is missing {sorted(missing)}")
    for column in ("shadow_id", "source_version", "input_hash", "decision_date", "as_of_date", "state_reason"):
        if str(sidecar[column]) != _one_value(frame, column):
            raise ValueError(f"sidecar {column} does not match target")
    if abs(float(sidecar["weight_sum"]) - float(pd.to_numeric(frame["weight"]).sum())) > 1e-8:
        raise ValueError("sidecar weight_sum does not match target")


def stage_target(
    source_parquet: Path,
    source_sidecar: Path,
    target_dir: Path,
    *,
    as_at: date | None = None,
    max_age_days: int = MAX_TARGET_AGE_DAYS,
) -> StagedTarget:
    """Validate both artifacts then atomically install their fixed latest names."""
    source_parquet = Path(source_parquet)
    source_sidecar = Path(source_sidecar)
    if not source_parquet.is_file() or not source_sidecar.is_file():
        raise ValueError("both source parquet and sidecar JSON must exist")
    frame = pd.read_parquet(source_parquet)
    sidecar = json.loads(source_sidecar.read_text(encoding="utf-8"))
    today = as_at or date.today()
    shadow_id, decision, input_hash = _validate_frame(frame, today, max_age_days)
    _validate_sidecar(frame, sidecar)

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    destination_parquet = target_dir / f"{shadow_id}_latest.parquet"
    destination_sidecar = target_dir / f"{shadow_id}_latest.json"
    parquet_tmp = destination_parquet.with_suffix(".parquet.tmp")
    sidecar_tmp = destination_sidecar.with_suffix(".json.tmp")
    frame.to_parquet(parquet_tmp, index=False)
    sidecar_tmp.write_text(json.dumps(sidecar, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    parquet_tmp.replace(destination_parquet)
    sidecar_tmp.replace(destination_sidecar)
    return StagedTarget(shadow_id, destination_parquet, destination_sidecar, len(frame), decision, input_hash)
