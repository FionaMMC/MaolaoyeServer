"""Recompute aa6b60d weights only from authenticated frozen QMT model data.

No live broker/network. Writes derived model files to temporary storage, result
to explicit local output. It does not use raw prices as model fallback.
"""

import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from audit_hydra_trade_evidence import sha  # network denial
from app.schemas.hydra_data import HydraDataManifest
from app.services.hydra_data import HydraDataStore


def run(research, batches, target_path):
    import pandas as pd

    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=research, text=True
    ).strip()
    assert commit == "aa6b60deef44b244764385e7b6bd681429b9b362"
    assert not subprocess.check_output(["git", "diff", "HEAD", "--"], cwd=research), (
        "Research tracked source is dirty"
    )
    target = json.loads(target_path.read_text(encoding="utf-8-sig"))
    with tempfile.TemporaryDirectory(prefix="hydra-frozen-research-") as temporary:
        root = Path(temporary)
        store = HydraDataStore(root / "verified")
        installed = {}
        for key, digest in target["input_hashes"].items():
            stream = "hydra_" + key
            path = batches / stream / digest
            manifest = HydraDataManifest.model_validate_json(
                (path / "manifest.json").read_text()
            )
            installed[key] = store.install(
                (path / "data.parquet").read_bytes(), manifest
            ).model_dump(mode="json")
        model, manifest = store.load(
            "hydra_model_hfq", target["input_hashes"]["model_hfq"]
        )
        flat = root / "flat" / "etf_back"
        flat.mkdir(parents=True)
        empty = root / "empty"
        empty.mkdir()
        for symbol, rows in model.groupby("symbol"):
            rows.drop(columns="symbol").to_parquet(
                flat / f"{symbol}.parquet", index=False
            )
        os.environ["V48_FLAT_DATA_DIR"] = str(flat.parent)
        os.environ["V48_DATA_DIR"] = str(empty)
        os.environ["V48_BACKTEST_END"] = "2026-09-03"
        sys.path[:0] = [str(research / "v48"), str(research)]
        builder = importlib.import_module("build_v481_rb_weights")
        weights, audit = builder.build_weights(pd.Timestamp("2026-09-03"))
        assert weights.index.max().normalize() == pd.Timestamp("2026-09-03")
        row = weights.loc[pd.Timestamp("2026-09-03")]
        codes = builder.ETF_CODES
        got = {codes[name]: float(weight) for name, weight in row.items()}
        expected = {w["code"]: w["weight"] for w in target["weights"]}
        assert set(got) == set(expected), "Research target universe changed"
        differences = {
            code: got.get(code, 0) - weight for code, weight in expected.items()
        }
        maxdiff = max(abs(x) for x in differences.values())
        return {
            "status": "MATCH" if maxdiff < 1e-10 else "DIVERGENCE",
            "research_commit": commit,
            "input_byte_hashes_verified": list(installed),
            "model_row_count": len(model),
            "model_symbols": sorted(model.symbol.unique()),
            "schedule_rows": len(weights),
            "weights": got,
            "expected_weights": expected,
            "differences": differences,
            "max_abs_weight_difference": maxdiff,
            "budget_row": audit.iloc[-1].to_dict(),
            "target_request_sha256": sha(target_path.read_bytes()),
            "input_hashes": target["input_hashes"],
            "preparation": "Split authenticated HFQ rows by symbol into isolated etf_back files; no other market source available.",
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--research", type=Path, required=True)
    p.add_argument("--batches", type=Path, required=True)
    p.add_argument("--target", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = run(a.research, a.batches, a.target)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    if result["status"] != "MATCH":
        raise SystemExit(1)
