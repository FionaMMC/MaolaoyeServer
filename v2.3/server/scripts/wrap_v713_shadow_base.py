"""Wrap the verified executable basket without rerunning stock selection."""
import argparse
import json
from pathlib import Path

from plugins.v713_relay import V713RelayAdapter


def wrap(source_dir: Path, source_version: str) -> dict:
    class Candidate(V713RelayAdapter):
        data_dir = source_dir
    frame = Candidate()._read_latest_basket()
    if frame is None:
        raise ValueError("missing executable V7.13 target")
    shadow = frame[["code", "weight", "decision_date", "as_of_date"]].copy()
    shadow["shadow_id"] = "Shadow_Base"
    shadow["state_reason"] = "BASE:" + str(frame.sleeve.iloc[0])
    shadow["source_version"] = source_version
    shadow["input_hash"] = str(frame.basket_sha256.iloc[0])
    manifest = {key: str(shadow[key].iloc[0]) for key in (
        "shadow_id", "decision_date", "as_of_date", "source_version", "input_hash",
    )}
    manifest.update(weight_sum=float(shadow.weight.sum()),
                    provenance="verified_v713_executable_basket",
                    executable_basket_sha256=manifest["input_hash"])
    shadow.to_parquet(source_dir / "Shadow_Base_latest.parquet", index=False)
    (source_dir / "Shadow_Base_latest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--source-version", required=True)
    args = parser.parse_args()
    print(json.dumps(wrap(args.source_dir, args.source_version)))
