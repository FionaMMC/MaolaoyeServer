"""从独立 live QMT userdata 冻结 HFQ/raw/公司行动/交易日历候选包。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from live_client.config import LiveClientConfig

PRICE_COLUMNS = [
    "symbol", "trade_date", "open", "high", "low", "close",
    "volume", "amount", "suspendFlag",
]


def _normalize_market_frame(symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.reset_index().copy()
    if "trade_date" not in data:
        data.rename(columns={data.columns[0]: "trade_date"}, inplace=True)
    dates = data["trade_date"]
    if pd.api.types.is_numeric_dtype(dates) and not dates.empty and dates.iloc[0] > 1e10:
        data["trade_date"] = pd.to_datetime(dates, unit="ms").dt.strftime("%Y%m%d")
    else:
        data["trade_date"] = dates.astype(str).str.replace("-", "", regex=False)
    data["symbol"] = symbol
    if "suspendFlag" not in data:
        data["suspendFlag"] = 0
    return data[PRICE_COLUMNS]


def _write_bundle(
    frame: pd.DataFrame,
    destination: Path,
    *,
    stream: str,
    adjustment: str,
    as_of_date: str,
    producer_commit: str,
    source: str = "qmt",
) -> dict:
    if frame.empty and stream != "hydra_corporate_actions":
        raise ValueError(f"{stream} 数据为空")
    if stream == "hydra_trading_calendar":
        required = {"trade_date"}
        date_column = "trade_date"
    elif stream == "hydra_corporate_actions":
        required = {
            "symbol", "event_date", "event_type", "cash_per_share",
            "share_factor", "source_event_id",
        }
        date_column = "event_date"
    else:
        required = set(PRICE_COLUMNS)
        date_column = "trade_date"
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{stream} 缺少列: {missing}")
    dates = frame[date_column].astype(str).str.replace("-", "", regex=False)
    if stream == "hydra_trading_calendar":
        if as_of_date not in set(dates):
            raise ValueError("trading calendar 不含 as_of_date")
    elif stream == "hydra_corporate_actions":
        if not dates.empty and dates.max() > as_of_date:
            raise ValueError("corporate action 晚于 as_of_date")
    elif dates.max() != as_of_date:
        raise ValueError(f"{stream} 最大日期不是 as_of_date")
    destination.mkdir(parents=True, exist_ok=False)
    temp = tempfile.NamedTemporaryFile(
        dir=destination, prefix=".data-", suffix=".parquet.tmp", delete=False,
    )
    temp.close()
    temp_path = Path(temp.name)
    data_path = destination / "data.parquet"
    try:
        frame.to_parquet(temp_path, index=False)
        body = temp_path.read_bytes()
        file_sha = hashlib.sha256(body).hexdigest()
        os.replace(temp_path, data_path)
    finally:
        temp_path.unlink(missing_ok=True)
    manifest = {
        "schema_version": 1,
        "stream": stream,
        "source": source,
        "adjustment": adjustment,
        "as_of_date": as_of_date,
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "producer_commit": producer_commit,
        "file_sha256": file_sha,
        "row_count": len(frame),
        "symbol_count": 0 if stream == "hydra_trading_calendar" else frame["symbol"].nunique(),
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def collect_prices(cfg: LiveClientConfig, as_of_date: str) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    from xtquant import xtdata

    xtdata.data_dir = str(cfg.userdata_dir)
    symbols = sorted(cfg.allowed_symbols)
    xtdata.download_history_data2(symbols, "1d", "", as_of_date)
    fields = ["open", "high", "low", "close", "volume", "amount", "suspendFlag"]
    frames = {}
    for stream, adjustment in (("hfq", "back"), ("raw", "none")):
        payload = xtdata.get_market_data_ex(
            fields, symbols, "1d", "", as_of_date,
            dividend_type=adjustment, fill_data=False,
        ) or {}
        missing = sorted(set(symbols) - set(payload))
        if missing:
            raise RuntimeError(f"QMT {stream} 缺标的: {missing}")
        frames[stream] = pd.concat(
            [_normalize_market_frame(symbol, payload[symbol]) for symbol in symbols],
            ignore_index=True,
        )
        if frames[stream]["trade_date"].max() != as_of_date:
            raise RuntimeError(f"QMT {stream} 最大日期不是 as_of_date")
    calendar = xtdata.get_trading_calendar(
        "SH", start_time=f"{as_of_date[:4]}0101", end_time=f"{int(as_of_date[:4]) + 1}1231",
    )
    if not calendar or as_of_date not in calendar:
        raise RuntimeError("QMT 交易日历不含 as_of_date")
    return frames["hfq"], frames["raw"], list(calendar)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--producer-commit", required=True)
    parser.add_argument("--corporate-actions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.producer_commit):
        raise ValueError("producer-commit 必须是 full Git SHA")
    cfg = LiveClientConfig.from_env()
    if args.output.exists():
        raise RuntimeError("output 已存在，拒绝覆盖数据冻结包")
    args.output.mkdir(parents=True)
    hfq, raw, calendar = collect_prices(cfg, args.as_of)
    actions = pd.read_parquet(args.corporate_actions)
    manifests = {
        "model_hfq": _write_bundle(
            hfq, args.output / "model_hfq", stream="hydra_model_hfq",
            adjustment="back", as_of_date=args.as_of,
            producer_commit=args.producer_commit,
        ),
        "execution_raw": _write_bundle(
            raw, args.output / "execution_raw", stream="hydra_execution_raw",
            adjustment="none", as_of_date=args.as_of,
            producer_commit=args.producer_commit,
        ),
        "corporate_actions": _write_bundle(
            actions, args.output / "corporate_actions",
            stream="hydra_corporate_actions", adjustment="corporate_actions",
            as_of_date=args.as_of, producer_commit=args.producer_commit,
        ),
        "trading_calendar": _write_bundle(
            pd.DataFrame({"trade_date": calendar}),
            args.output / "trading_calendar",
            stream="hydra_trading_calendar", adjustment="calendar",
            as_of_date=args.as_of, producer_commit=args.producer_commit,
        ),
    }
    calendar_body = (
        json.dumps(calendar, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()
    (args.output / "trading_calendar.json").write_bytes(calendar_body)
    result = {
        "as_of_date": args.as_of,
        "manifests": manifests,
        "trading_calendar_sha256": manifests["trading_calendar"]["file_sha256"],
    }
    (args.output / "snapshot.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
