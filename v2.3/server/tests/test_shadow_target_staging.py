from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from app.shadow.target_staging import SERVER_COLUMNS, stage_target


def _target(shadow_id: str = "Shadow_Base") -> pd.DataFrame:
    source_version = "a" * 40
    if shadow_id == "Shadow_Aux_Hard_TOP2":
        source_version = "v7.9-hard-logistic-aux-top2-r1@" + "a" * 40
    elif shadow_id == "Shadow_ML_TOP2":
        source_version = "sw2021-aux-top2-logistic-20260626-r1@" + "b" * 64 + ":" + "a" * 40
    elif shadow_id == "Shadow_Hydra_V481_RB":
        source_version = "v48.1-RB@" + "a" * 40
    return pd.DataFrame({
        "shadow_id": [shadow_id], "code": ["511260.SH"], "weight": [1.0],
        "decision_date": ["20260724"], "as_of_date": ["20260724"],
        "state_reason": ["BASE:T1_5050"], "source_version": [source_version],
        "input_hash": ["c" * 64],
    })[SERVER_COLUMNS]


def _write_pair(tmp_path, frame: pd.DataFrame):
    parquet = tmp_path / "source.parquet"
    sidecar = tmp_path / "source.json"
    frame.to_parquet(parquet, index=False)
    sidecar.write_text(json.dumps({
        "shadow_id": frame["shadow_id"].iloc[0], "source_version": frame["source_version"].iloc[0],
        "input_hash": frame["input_hash"].iloc[0], "decision_date": "20260724", "as_of_date": "20260724",
        "weight_sum": 1.0, "state_reason": frame["state_reason"].iloc[0],
    }), encoding="utf-8")
    return parquet, sidecar


def test_stage_target_requires_exact_contract_and_installs_fixed_names(tmp_path):
    frame = _target()
    parquet, sidecar = _write_pair(tmp_path, frame)
    staged = stage_target(parquet, sidecar, tmp_path / "installed", as_at=date(2026, 7, 25))
    assert staged.shadow_id == "Shadow_Base"
    assert staged.parquet_path.name == "Shadow_Base_latest.parquet"
    assert pd.read_parquet(staged.parquet_path).equals(frame)


def test_stage_target_rejects_sidecar_mismatch(tmp_path):
    parquet, sidecar = _write_pair(tmp_path, _target())
    payload = json.loads(sidecar.read_text())
    payload["input_hash"] = "d" * 64
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        stage_target(parquet, sidecar, tmp_path / "installed", as_at=date(2026, 7, 25))


def test_stage_target_rejects_future_or_stale_dates(tmp_path):
    frame = _target()
    frame.loc[:, "decision_date"] = "20260726"
    frame.loc[:, "as_of_date"] = "20260726"
    parquet, sidecar = _write_pair(tmp_path, frame)
    payload = json.loads(sidecar.read_text())
    payload.update({"decision_date": "20260726", "as_of_date": "20260726"})
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="future"):
        stage_target(parquet, sidecar, tmp_path / "installed", as_at=date(2026, 7, 25))
