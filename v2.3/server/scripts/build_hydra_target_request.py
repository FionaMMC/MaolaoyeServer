"""把已审计 Hydra target + 双数据 snapshot 转成 relay JSON contract。"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import pandas as pd

from app.schemas.hydra_relay import HydraTargetRequest, hydra_basket_hash


def _read(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def _single_value(frame: pd.DataFrame, column: str, *, date: bool = False) -> str:
    values = frame[column]
    if values.isna().any():
        raise ValueError(f"target {column} 含空值")
    normalized = values.astype(str)
    if date:
        normalized = normalized.str.replace("-", "", regex=False)
    unique = normalized.unique().tolist()
    if len(unique) != 1:
        raise ValueError(f"target {column} 必须全表一致: {unique}")
    return unique[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--data-snapshot", type=Path, required=True)
    parser.add_argument("--execution-date", required=True)
    parser.add_argument("--execution-domain", choices=("paper", "live"), required=True)
    parser.add_argument("--account-alias", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--cash-buffer", type=float, default=0.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    target = _read(args.target)
    required = {
        "code", "weight", "decision_date", "as_of_date", "source_version",
        "input_hash",
    }
    missing = sorted(required - set(target.columns))
    if missing:
        raise ValueError(f"target 缺少列: {missing}")
    if target.empty:
        raise ValueError("target 为空")
    decision_date = _single_value(target, "decision_date", date=True)
    as_of_date = _single_value(target, "as_of_date", date=True)
    strategy_version = _single_value(target, "source_version")
    publisher_input_hash = _single_value(target, "input_hash")
    if not re.fullmatch(r"[0-9a-f]{64}", publisher_input_hash):
        raise ValueError("target input_hash 必须是 lowercase SHA-256")
    sidecar = json.loads(args.sidecar.read_text(encoding="utf-8"))
    snapshot = json.loads(args.data_snapshot.read_text(encoding="utf-8"))
    publisher = str(sidecar.get("publisher_source_commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", publisher):
        raise ValueError("sidecar publisher_source_commit 必须是 full SHA")
    for key, expected in (
        ("source_version", strategy_version),
        ("decision_date", decision_date),
        ("as_of_date", as_of_date),
        ("input_hash", publisher_input_hash),
    ):
        actual = str(sidecar.get(key, ""))
        if key in {"decision_date", "as_of_date"}:
            actual = actual.replace("-", "")
        if actual != expected:
            raise ValueError(f"sidecar {key} 与 target 不一致")
    research_hashes = sidecar.get("input_hashes")
    if not isinstance(research_hashes, dict) or not research_hashes:
        raise ValueError("sidecar input_hashes 缺失或为空")
    if not all(
        key and re.fullmatch(r"[0-9a-f]{64}", str(value))
        for key, value in research_hashes.items()
    ):
        raise ValueError("sidecar input_hashes 必须是 lowercase SHA-256 映射")
    weight_sum = float(sidecar.get("weight_sum", float("nan")))
    if not math.isfinite(weight_sum) or abs(weight_sum - float(target["weight"].sum())) > 1e-8:
        raise ValueError("sidecar weight_sum 与 target 不一致")
    if str(snapshot.get("as_of_date", "")).replace("-", "") != as_of_date:
        raise ValueError("data snapshot as_of_date 与 target 不一致")
    manifests = snapshot["manifests"]
    if any(
        str(manifests[name].get("as_of_date", "")).replace("-", "") != as_of_date
        for name in (
            "model_hfq", "execution_raw", "corporate_actions", "trading_calendar",
        )
    ):
        raise ValueError("data manifest as_of_date 与 target 不一致")
    payload = {
        "execution_domain": args.execution_domain,
        "account_alias": args.account_alias,
        "instance_id": args.instance_id,
        "strategy_version": strategy_version,
        "publisher_source_commit": publisher,
        "decision_date": decision_date,
        "as_of_date": as_of_date,
        "execution_date": args.execution_date,
        "research_input_hashes": research_hashes,
        "input_hashes": {
            "model_hfq": manifests["model_hfq"]["file_sha256"],
            "execution_raw": manifests["execution_raw"]["file_sha256"],
            "corporate_actions": manifests["corporate_actions"]["file_sha256"],
            "trading_calendar": manifests["trading_calendar"]["file_sha256"],
        },
        "weights": [
            {"code": str(row.code), "weight": float(row.weight)}
            for row in target[["code", "weight"]].itertuples(index=False)
        ],
        "cash_buffer_weight": args.cash_buffer,
        "buy_price_offset_bps": 50.0,
        "sell_price_offset_bps": 50.0,
    }
    payload["basket_sha256"] = hydra_basket_hash(payload)
    validated = HydraTargetRequest(**payload)
    if args.output.exists():
        raise RuntimeError("output 已存在，拒绝覆盖 target request")
    args.output.write_text(
        json.dumps(validated.model_dump(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output),
        "basket_sha256": validated.basket_sha256,
        "weights": len(validated.weights),
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
