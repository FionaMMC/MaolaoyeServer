"""V7.13 target-basket relay with content-addressed idempotency.

The strategy research repository produces a single Parquet target.  This
adapter never recomputes TOP50, Hydra or a switching signal: it verifies the
published contract and only turns the verified weights into QMT diffs.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from pathlib import Path
from typing import ClassVar

import pandas as pd
import yaml

from app.strategy.base import RawSignal
from app.strategy.context import Context
from plugins.v79_relay import V79RelayAdapter

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_V713_DIR = _HERE / "v713"
_REQUIRED_COLUMNS = {
    "code", "weight", "strategy_version", "sleeve", "decision_date",
    "as_of_date", "basket_sha256",
}
_ALLOWED_SLEEVES = {"TOP50", "T1_5050", "AUX_HYDRA"}
_CODE_PATTERN = re.compile(r"^\d{6}\.(SH|SZ)$")


def basket_hash(frame: pd.DataFrame) -> str:
    canonical = frame[
        ["code", "weight", "strategy_version", "sleeve", "decision_date", "as_of_date"]
    ].sort_values("code")
    payload = canonical.to_csv(
        index=False, lineterminator="\n", float_format="%.12g"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class V713RelayAdapter(V79RelayAdapter):
    """Consume only a verified V7.13 executable basket."""

    name = "v713_relay"
    data_dir: ClassVar[Path | None] = _V713_DIR / "data"
    data_files: ClassVar[list[str]] = ["v713_target_latest.parquet"]
    _cfg: ClassVar[dict | None] = None

    def _load_config(self) -> None:
        if type(self)._cfg is None:
            with (_V713_DIR / "config.yaml").open(encoding="utf-8") as handle:
                type(self)._cfg = yaml.safe_load(handle)

    def _read_latest_basket(self) -> pd.DataFrame | None:
        data_dir = type(self).data_dir
        if data_dir is None:
            return None
        path = Path(data_dir) / "v713_target_latest.parquet"
        if not path.exists():
            return None
        frame = pd.read_parquet(path)
        if frame.empty:
            return None
        missing = _REQUIRED_COLUMNS - set(frame.columns)
        if missing:
            raise ValueError(f"V7.13 target misses required columns: {sorted(missing)}")
        if frame["code"].isna().any() or (frame["code"].astype(str).str.strip() == "").any():
            raise ValueError("V7.13 target contains an empty code")
        frame = frame.copy()
        frame["code"] = frame["code"].astype(str).str.strip()
        if frame["code"].duplicated().any():
            raise ValueError("V7.13 target contains duplicate codes")
        if not frame["code"].map(lambda value: bool(_CODE_PATTERN.fullmatch(value))).all():
            raise ValueError("V7.13 target contains a non-tradeable code")
        frame["weight"] = pd.to_numeric(frame["weight"], errors="coerce")
        if (frame["weight"].isna().any()
                or not frame["weight"].map(math.isfinite).all()
                or (frame["weight"] <= 0).any()):
            raise ValueError("V7.13 target weights must be finite and positive")
        if abs(float(frame["weight"].sum()) - 1.0) > 1e-6:
            raise ValueError("V7.13 target weights must sum to 1")
        for column, expected in (("strategy_version", "v7.13-base"),):
            values = frame[column].drop_duplicates().tolist()
            if values != [expected]:
                raise ValueError(f"V7.13 target has invalid {column}: {values}")
        for column in ("decision_date", "as_of_date", "sleeve", "basket_sha256"):
            if frame[column].nunique(dropna=False) != 1:
                raise ValueError(f"V7.13 target must have one {column}")
        sleeve = str(frame["sleeve"].iloc[0])
        if sleeve not in _ALLOWED_SLEEVES:
            raise ValueError(f"V7.13 target has forbidden sleeve: {sleeve}")
        decision_date = pd.to_datetime(
            str(frame["decision_date"].iloc[0]), format="%Y%m%d", errors="raise"
        )
        as_of_date = pd.to_datetime(
            str(frame["as_of_date"].iloc[0]), format="%Y%m%d", errors="raise"
        )
        if as_of_date > decision_date:
            raise ValueError("V7.13 target as_of_date is after decision_date")
        expected_hash = str(frame["basket_sha256"].iloc[0])
        if basket_hash(frame) != expected_hash:
            raise ValueError("V7.13 target basket_sha256 mismatch")
        return frame

    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        target = pd.to_datetime(str(trade_date), format="%Y%m%d")
        self._load_config()
        cfg = self._cfg or {}
        basket = self._read_latest_basket()
        if basket is None:
            logger.warning("V7.13 basket missing or empty; skip")
            return []

        decision_date = str(basket["decision_date"].iloc[0])
        if target < pd.to_datetime(decision_date, format="%Y%m%d"):
            logger.warning("V7.13 target decision date %s is in the future; skip", decision_date)
            return []
        basket_id = str(basket["basket_sha256"].iloc[0])
        state = ctx.strategy_state()
        guard = ctx.execution_guard()
        if not cfg.get("dry_run", True) and not guard.get("allowed", False):
            blockers = list(guard.get("blockers") or ["missing_execution_guard"])
            next_state = dict(state)
            next_state.update({
                "last_blocked_basket_sha256": basket_id,
                "last_execution_blockers": blockers,
            })
            ctx.set_strategy_state(next_state)
            logger.error(
                "V7.13[%s] execution blocked basket=%s blockers=%s",
                ctx.instance_id, basket_id[:12], blockers,
            )
            return []
        if state.get("last_consumed_basket_sha256") == basket_id:
            logger.info("V7.13[%s] basket=%s already consumed; skip", ctx.instance_id, basket_id[:12])
            return []
        if cfg.get("dry_run", True) and state.get("last_replayed_basket_sha256") == basket_id:
            logger.info(
                "V7.13[%s] basket=%s already replayed in dry-run; skip",
                ctx.instance_id, basket_id[:12],
            )
            return []

        max_age_days = int(cfg.get("max_target_age_days", 7))
        decision_ts = pd.to_datetime(decision_date, format="%Y%m%d")
        if (target - decision_ts).days > max_age_days:
            raise ValueError(
                f"V7.13 target is stale by {(target - decision_ts).days} days "
                f"(max {max_age_days})"
            )

        weights = dict(zip(basket["code"].astype(str), basket["weight"].astype(float)))
        nav = self._compute_nav(ctx, target)
        target_qty = self._weights_to_quantities(weights, nav, ctx, target)
        target_qty = self._apply_risk_filters(ctx, target_qty, target)
        signals = self._diff_and_emit(ctx, ctx.positions(), target_qty, target)

        if cfg.get("dry_run", True):
            next_state = dict(state)
            next_state.update({
                "last_replayed_basket_sha256": basket_id,
                "last_replayed_decision_date": decision_date,
                "last_replayed_sleeve": str(basket["sleeve"].iloc[0]),
                "last_target_quantities": target_qty,
            })
            ctx.set_strategy_state(next_state)
            logger.info(
                "V7.13[%s] DRY-RUN decision=%s sleeve=%s basket=%s target=%d signals=%d",
                ctx.instance_id, decision_date, basket["sleeve"].iloc[0], basket_id[:12],
                len(target_qty), len(signals),
            )
            return []

        next_state = dict(state)
        next_state.update({
            "last_consumed_basket_sha256": basket_id,
            "last_consumed_decision_date": decision_date,
            "last_consumed_sleeve": str(basket["sleeve"].iloc[0]),
            "last_target_weights": weights,
            "last_target_quantities": target_qty,
            "last_target_as_of_date": str(basket["as_of_date"].iloc[0]),
            "reconciliation_status": "pending",
        })
        ctx.set_strategy_state(next_state)
        logger.info("V7.13[%s] emitted %d signals for basket=%s", ctx.instance_id, len(signals), basket_id[:12])
        return signals
