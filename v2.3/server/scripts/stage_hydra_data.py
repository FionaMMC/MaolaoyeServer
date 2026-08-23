"""离线校验并安装一个不可变 Hydra 数据批次。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.schemas.hydra_data import HydraDataManifest
from app.services.hydra_data import HydraDataStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parquet", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()

    manifest = HydraDataManifest.model_validate_json(
        args.manifest.read_text(encoding="utf-8")
    )
    result = HydraDataStore(args.root).install(
        args.parquet.read_bytes(), manifest,
    )
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
