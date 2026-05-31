"""V53 e2e integration — registry → pipeline → adapter → orders DB.

Goes through load_plugins() to get V53Adapter rather than direct import, validating
auto-discovery + pipeline registration path used in production.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from app.storage.parquet import ParquetStore
from app.strategy.base import Strategy
from app.strategy.context import Context
from app.strategy.loader import load_plugins


def _bdays_as_int(start: str, end: str) -> list[int]:
    return [int(d.strftime("%Y%m%d")) for d in pd.bdate_range(start, end)]


@pytest.fixture
def v53_strategy_class():
    """通过 load_plugins() 拿 V53Adapter (而不是直接 import)。
    模拟 production scheduler/pipeline.py 的注册路径。"""
    plugins_dir = Path(__file__).resolve().parent.parent.parent / "plugins"
    reg = load_plugins(plugins_dir)
    assert "v53" in reg, f"v53 not auto-registered; got: {sorted(reg)}"
    return reg["v53"]


def _make_ctx(tmp_path: Path, trade_date_int: int,
              anchor_days: list[int] | None = None) -> Context:
    store = ParquetStore(root=tmp_path / "parquet")
    if anchor_days:
        store.append("etfs", "510300.SH", pd.DataFrame({
            "trade_date": anchor_days,
            "open": [3.0] * len(anchor_days),
            "high": [3.0] * len(anchor_days),
            "low":  [3.0] * len(anchor_days),
            "close": [3.0] * len(anchor_days),
            "volume": [1_000_000] * len(anchor_days),
        }))
    return Context(
        instance_id="paper_v53_v53",
        trade_date=trade_date_int,
        virtual_cash=10_000_000.0,
        virtual_positions={},
        parquet_store=store,
    )


def test_e2e_non_month_end_returns_empty(v53_strategy_class, tmp_path):
    """非月末 → 0 RawSignals，无 orders 落库"""
    days = _bdays_as_int("2024-04-01", "2024-04-30")
    ctx = _make_ctx(tmp_path, 20240415, anchor_days=days)
    adapter: Strategy = v53_strategy_class()
    sigs = adapter.run(ctx, 20240415)
    assert sigs == []


def test_e2e_month_end_dry_run_logs_and_returns_empty(v53_strategy_class, tmp_path, caplog):
    """调仓日（新月首交易日）+ dry_run=true 配置 → DRY-RUN log + return []"""
    import logging
    # 该 e2e 走 load_plugins 真实路径，须有真实 bundle 才能算到 DRY-RUN 这步。
    # bundle 被 gitignore（refresh_v53_bundle.sh 生成），无则 skip。
    bundle = Path(__file__).resolve().parent.parent.parent / "plugins" / "v53" / "data" / "etf_close.parquet"
    if not bundle.exists():
        pytest.skip(f"真实 bundle 缺失，跳过 e2e: {bundle}")
    # anchor 数据只到 4/30；target=5/6（新月首交易日）→ 调仓日
    days = _bdays_as_int("2024-04-01", "2024-04-30")
    target_int = 20240506
    ctx = _make_ctx(tmp_path, target_int, anchor_days=days)
    adapter: Strategy = v53_strategy_class()
    caplog.set_level(logging.INFO)
    sigs = adapter.run(ctx, target_int)

    # dry_run=true → 0 signals
    assert sigs == []
    # 但有 DRY-RUN log（来自 plugins.v53_adapter logger 或 _qmt_plugin_v53_adapter）
    log_msgs = [r.getMessage() for r in caplog.records]
    assert any("DRY-RUN" in m for m in log_msgs), \
        f"no DRY-RUN log; messages: {log_msgs}"
