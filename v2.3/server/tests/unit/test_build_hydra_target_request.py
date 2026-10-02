"""Hydra publisher target/sidecar/data snapshot 的端到端血缘校验。"""
from __future__ import annotations

import json
import sys

import pandas as pd
import pytest

from app.schemas.hydra_relay import HydraTargetRequest
from scripts.build_hydra_target_request import main


def _inputs(tmp_path):
    target_path = tmp_path / "target.parquet"
    sidecar_path = tmp_path / "target.json"
    snapshot_path = tmp_path / "snapshot.json"
    target = pd.DataFrame({
        "code": ["510300.SH", "159915.SZ"],
        "weight": [0.6, 0.4],
        "decision_date": ["20260731", "20260731"],
        "as_of_date": ["20260731", "20260731"],
        "source_version": ["v48.1-RB@test", "v48.1-RB@test"],
        "input_hash": ["8" * 64, "8" * 64],
    })
    target.to_parquet(target_path, index=False)
    sidecar_path.write_text(json.dumps({
        "publisher_source_commit": "7" * 40,
        "source_version": "v48.1-RB@test",
        "decision_date": "20260731",
        "as_of_date": "20260731",
        "input_hash": "8" * 64,
        "input_hashes": {"weights": "9" * 64, "signals": "a" * 64},
        "weight_sum": 1.0,
    }), encoding="utf-8")
    snapshot_path.write_text(json.dumps({
        "as_of_date": "20260731",
        "manifests": {
            name: {"file_sha256": char * 64, "as_of_date": "20260731"}
            for name, char in (
                ("model_hfq", "1"),
                ("execution_raw", "2"),
                ("corporate_actions", "3"),
                ("trading_calendar", "4"),
            )
        },
    }), encoding="utf-8")
    return target_path, sidecar_path, snapshot_path


def _argv(target, sidecar, snapshot, output):
    return [
        "build_hydra_target_request",
        "--target", str(target),
        "--sidecar", str(sidecar),
        "--data-snapshot", str(snapshot),
        "--execution-date", "20260803",
        "--execution-domain", "paper",
        "--account-alias", "hydra-paper",
        "--instance-id", "paper_hydra",
        "--output", str(output),
    ]


def test_builder_preserves_research_and_execution_input_hashes(tmp_path, monkeypatch):
    target, sidecar, snapshot = _inputs(tmp_path)
    output = tmp_path / "request.json"
    monkeypatch.setattr(sys, "argv", _argv(target, sidecar, snapshot, output))
    main()
    request = HydraTargetRequest.model_validate_json(
        output.read_text(encoding="utf-8")
    )
    assert request.research_input_hashes == {
        "weights": "9" * 64,
        "signals": "a" * 64,
    }
    assert set(request.input_hashes) == {
        "model_hfq", "execution_raw", "corporate_actions", "trading_calendar",
    }


def test_builder_rejects_mixed_target_metadata(tmp_path, monkeypatch):
    target, sidecar, snapshot = _inputs(tmp_path)
    frame = pd.read_parquet(target)
    frame.loc[1, "decision_date"] = "20260730"
    frame.to_parquet(target, index=False)
    output = tmp_path / "request.json"
    monkeypatch.setattr(sys, "argv", _argv(target, sidecar, snapshot, output))
    with pytest.raises(ValueError, match="全表一致"):
        main()
