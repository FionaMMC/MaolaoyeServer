"""Monthly read-only data collection. Strategy computation stays on the server."""

import base64
import json
import os
from pathlib import Path
import tempfile

import pandas as pd

from live_client.data_snapshot import (
    ResearchDataConfig,
    EXECUTABLE_SYMBOLS,
    RESEARCH_ONLY_SYMBOLS,
    collect_prices,
    collect_corporate_actions,
    _write_bundle,
)
from live_client.execution_publication import producer_commit

FIRST_MONTHLY_DATE = "20260918"  # Do not regenerate pre-deployment monthly targets.


def latest_completed_month_end(calendar, reference_date):
    dates = sorted(set(calendar))
    eligible = [
        day
        for day, following in zip(dates, dates[1:])
        if FIRST_MONTHLY_DATE <= day <= reference_date and day[:6] != following[:6]
    ]
    return eligible[-1] if eligible else None


def publish_monthly_data(cfg, reference_date, server, xtdata=None):
    if xtdata is None:
        from xtquant import xtdata
    xtdata.data_dir = str(cfg.userdata_dir)
    calendar = xtdata.get_trading_calendar(
        "SH",
        start_time=f"{int(reference_date[:4]) - 1}0101",
        end_time=f"{int(reference_date[:4]) + 1}1231",
    )
    if not calendar:
        raise RuntimeError("月度研究缺 QMT 交易日历")
    day = latest_completed_month_end(calendar, reference_date)
    if day is None:
        return {"status": "NO_MONTHLY_RESEARCH_DUE"}
    # Per-account/instance namespace; never reuse another account's receipt.
    import hashlib

    scope = hashlib.sha256(
        f"{cfg.execution_domain}/{cfg.account_alias}/{cfg.instance_id}".encode()
    ).hexdigest()[:24]
    parent = Path(cfg.state_db).parent / "monthly-research" / scope
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / day
    receipt_path = root / "upload-receipt.json"
    if receipt_path.exists():
        return json.loads(receipt_path.read_text(encoding="utf-8"))
    if not root.exists():
        if day != reference_date:
            raise RuntimeError(
                "月末当日未冻结研究数据；禁止用后来的复权历史冒充当时快照，请人工补录获批冻结包"
            )
        with tempfile.TemporaryDirectory(dir=parent, prefix=".freeze-") as temporary:
            staged = Path(temporary) / day
            staged.mkdir()
            research = ResearchDataConfig(Path(cfg.userdata_dir))
            research.validate()
            hfq, raw, dates = collect_prices(research, day)
            actions = collect_corporate_actions(research, day, require_response=True)
            commit = producer_commit()
            frames = {
                "model_hfq": (hfq, "back"),
                "execution_raw": (raw, "none"),
                "corporate_actions": (actions, "corporate_actions"),
                "trading_calendar": (pd.DataFrame({"trade_date": dates}), "calendar"),
            }
            for key, (frame, adjustment) in frames.items():
                # Empty action tables/calendar bytes can otherwise repeat across
                # months while their immutable manifest dates differ.
                frame.attrs["hydra_as_of_date"] = day
                _write_bundle(
                    frame,
                    staged / key,
                    stream="hydra_" + key,
                    adjustment=adjustment,
                    as_of_date=day,
                    producer_commit=commit,
                    source="qmt_xtdata",
                    executable_symbols=sorted(EXECUTABLE_SYMBOLS)
                    if key in {"model_hfq", "execution_raw"}
                    else None,
                    research_only_symbols=sorted(RESEARCH_ONLY_SYMBOLS)
                    if key == "model_hfq"
                    else None,
                )
            try:
                os.rename(staged, root)
            except OSError:
                if not root.is_dir():
                    raise
                # Another freeze won; send its immutable package below.
    streams = {}
    for key in ("model_hfq", "execution_raw", "corporate_actions", "trading_calendar"):
        streams[key] = {
            "manifest": json.loads(
                (root / key / "manifest.json").read_text(encoding="utf-8")
            ),
            "parquet_base64": base64.b64encode(
                (root / key / "data.parquet").read_bytes()
            ).decode(),
        }
    result = server._post(
        "/hydra/research/snapshots",
        dict(
            execution_domain=cfg.execution_domain,
            account_alias=cfg.account_alias,
            instance_id=cfg.instance_id,
            as_of_date=day,
            streams=streams,
        ),
    )
    if (
        result.get("status") != "RESEARCH_SNAPSHOT_STORED"
        or result.get("as_of_date") != day
    ):
        raise RuntimeError("月度冻结上传缺匹配回执")
    with tempfile.NamedTemporaryFile(
        dir=root, mode="w", encoding="utf-8", delete=False
    ) as file:
        json.dump(result, file, ensure_ascii=False)
        temp = Path(file.name)
    os.replace(temp, receipt_path)
    return result
