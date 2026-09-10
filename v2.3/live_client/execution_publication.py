"""Read daily unadjusted QMT bars/calendar for server execution publication."""

from pathlib import Path
import subprocess

import pandas as pd

from live_client.data_snapshot import _normalize_market_frame


def producer_commit():
    root = Path(__file__).resolve().parent.parent
    if len(root.name) == 40 and all(c in "0123456789abcdef" for c in root.name):
        return root.name  # Versioned installation; no Git needed on Windows.
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def collect_execution_publication(cfg, reference_date, xtdata=None):
    if xtdata is None:
        from xtquant import xtdata
    xtdata.data_dir = str(cfg.userdata_dir)
    calendar = xtdata.get_trading_calendar(
        "SH",
        start_time=reference_date,
        end_time=f"{int(reference_date[:4]) + 1}1231",
    )
    if not calendar:
        raise RuntimeError("QMT 未返回交易日历")
    if reference_date not in calendar:
        return None
    symbols = sorted(cfg.allowed_symbols)
    xtdata.download_history_data2(symbols, "1d", reference_date, reference_date)
    fields = ["open", "high", "low", "close", "volume", "amount", "suspendFlag"]
    payload = (
        xtdata.get_market_data_ex(
            fields,
            symbols,
            "1d",
            reference_date,
            reference_date,
            dividend_type="none",
            fill_data=False,
        )
        or {}
    )
    if set(payload) != set(symbols):
        raise RuntimeError("QMT 执行行情缺少白名单标的")
    raw = pd.concat(
        [_normalize_market_frame(symbol, payload[symbol]) for symbol in symbols],
        ignore_index=True,
    )
    if len(raw) != len(symbols) or set(raw.trade_date) != {reference_date}:
        raise RuntimeError("执行行情必须每标的一条当日不复权价格；不得前值填充")
    return dict(
        execution_domain=cfg.execution_domain,
        account_alias=cfg.account_alias,
        reference_date=reference_date,
        producer_commit=producer_commit(),
        bars=raw.to_dict(orient="records"),
        calendar_dates=list(calendar),
    )
