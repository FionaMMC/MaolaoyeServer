from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.stage_shadow_target import stage_target


SOURCE_VERSION = "v48.1-RB@49c16dadc298d6a51470bd5c2f931ecc36f65460"


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    destination = tmp_path / "targets" / "Shadow_Hydra_V481_RB_latest.parquet"
    config = tmp_path / "strategies.yaml"
    config.write_text(f"""
shadow_instances:
  - shadow_id: Shadow_Hydra_V481_RB
    mode: shadow
    orders_enabled: false
    target_file: {destination}
    max_target_age_days: 40
    allowed_symbols: [511260.SH]
    allowed_source_versions: [{SOURCE_VERSION}]
    require_sidecar: true
""", encoding="utf-8")
    payload = {
        "shadow_id": "Shadow_Hydra_V481_RB",
        "decision_date": "20260717",
        "source_version": SOURCE_VERSION,
        "input_hashes": {
            "weights": "a" * 64,
            "budget_audit": "b" * 64,
            "signals": "c" * 64,
            "training_audit": "d" * 64,
        },
        "target_bond_abs_risk_budget": 0.2,
        "trend_z": 0.0,
        "duration_score": 0.5,
        "signal_date": "20260630",
        "training_label_end": "20260630",
    }
    input_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    target = pd.DataFrame([{
        "shadow_id": "Shadow_Hydra_V481_RB",
        "code": "511260.SH",
        "weight": 1.0,
        "decision_date": "20260717",
        "as_of_date": "20260717",
        "state_reason": "DYNAMIC_BOND_RISK_BUDGET",
        "source_version": SOURCE_VERSION,
        "input_hash": input_hash,
    }])
    source = tmp_path / "shadow_hydra_v481_rb_20260717.parquet"
    target.to_parquet(source, index=False)
    sidecar = tmp_path / "shadow_hydra_v481_rb_20260717.json"
    sidecar.write_text(
        json.dumps({**payload, "as_of_date": "20260717",
                    "input_hash": input_hash,
                    "state_reason": "DYNAMIC_BOND_RISK_BUDGET",
                    "weight_sum": 1.0}),
        encoding="utf-8",
    )
    return config, source, sidecar


def test_stage_shadow_target_validates_before_atomic_install(tmp_path):
    config, source, sidecar = _write_fixture(tmp_path)

    checked = stage_target(
        source, config, "Shadow_Hydra_V481_RB", 20260725,
        sidecar=sidecar, install=False,
    )
    assert checked["validated"] is True
    assert checked["installed"] is False
    assert not Path(checked["destination"]).exists()

    installed = stage_target(
        source, config, "Shadow_Hydra_V481_RB", 20260725,
        sidecar=sidecar, install=True,
    )
    destination = Path(installed["destination"])
    assert installed["installed"] is True
    assert pd.read_parquet(destination).iloc[0]["source_version"] == SOURCE_VERSION
    assert destination.with_suffix(".json").exists()


def test_stage_shadow_target_rejects_missing_or_tampered_sidecar(tmp_path):
    config, source, sidecar = _write_fixture(tmp_path)
    with pytest.raises(ValueError, match="requires"):
        stage_target(
            source, config, "Shadow_Hydra_V481_RB", 20260725,
            install=True,
        )

    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["duration_score"] = -1.0
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="provenance hash"):
        stage_target(
            source, config, "Shadow_Hydra_V481_RB", 20260725,
            sidecar=sidecar, install=True,
        )
