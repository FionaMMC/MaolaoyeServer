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


def allocation_hash(frame: pd.DataFrame) -> str:
    """Identify the executable monthly allocation, excluding publication date.

    ``decision_date`` is artifact provenance, not a rebalance cadence key.  The
    producer may run more than once during a month, while V7.13 must keep using
    the same completed ``as_of_date`` allocation until a new month closes.
    """
    canonical = frame[
        ["code", "weight", "strategy_version", "sleeve", "as_of_date"]
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
        basket_as_of_date = str(basket["as_of_date"].iloc[0])
        allocation_id = allocation_hash(basket)
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
        consumed = state.get("last_consumed_basket_sha256") == basket_id
        if cfg.get("dry_run", True) and state.get("last_replayed_basket_sha256") == basket_id:
            logger.info(
                "V7.13[%s] basket=%s already replayed in dry-run; skip",
                ctx.instance_id, basket_id[:12],
            )
            return []

        max_age_days = int(cfg.get("max_target_age_days", 7))
        decision_ts = pd.to_datetime(decision_date, format="%Y%m%d")
        target_age_days = (target - decision_ts).days

        if consumed:
            # Content-addressed idempotency means "do not recompute a consumed
            # basket", not "abandon its unfilled remainder".  Reuse the exact
            # persisted target quantities and diff them against the settlement-
            # updated virtual ledger.  Fully filled baskets still emit nothing;
            # PARTIAL/CANCELLED batches emit only their residual quantity.
            if target_age_days > max_age_days:
                next_state = dict(state)
                next_state.update({
                    "last_residual_retry_blocked_trade_date": str(trade_date),
                    "last_residual_retry_blocked_reason": "stale_consumed_basket",
                })
                ctx.set_strategy_state(next_state)
                logger.warning(
                    "V7.13[%s] consumed basket=%s is stale by %d days; "
                    "skip residual retry",
                    ctx.instance_id, basket_id[:12], target_age_days,
                )
                return []
            saved_target = state.get("last_target_quantities")
            if not isinstance(saved_target, dict):
                logger.error(
                    "V7.13[%s] basket=%s consumed without persisted target; skip residual",
                    ctx.instance_id, basket_id[:12],
                )
                return []
            try:
                target_qty = {
                    str(code): int(quantity)
                    for code, quantity in saved_target.items()
                }
            except (TypeError, ValueError):
                logger.error(
                    "V7.13[%s] basket=%s has invalid persisted target; skip residual",
                    ctx.instance_id, basket_id[:12],
                )
                return []
            signals = self._diff_and_emit(ctx, ctx.positions(), target_qty, target)
            if not signals:
                logger.info(
                    "V7.13[%s] basket=%s already consumed and target reached; skip",
                    ctx.instance_id, basket_id[:12],
                )
                return []
            next_state = dict(state)
            next_state.update({
                "last_residual_retry_trade_date": str(trade_date),
                "last_residual_retry_signals": len(signals),
            })
            ctx.set_strategy_state(next_state)
            logger.warning(
                "V7.13[%s] retrying %d residual signals for consumed basket=%s",
                ctx.instance_id, len(signals), basket_id[:12],
            )
            return signals

        # V7.13 is monthly.  A weekly publisher may legitimately create a new
        # artifact with a later decision_date, but the relay must not turn that
        # metadata-only change into another rebalance for the same completed
        # month.  Exact consumed artifacts were handled above so their explicit
        # PARTIAL/CANCELLED residuals can still be completed.
        dry_run = bool(cfg.get("dry_run", True))
        cycle_state_key = (
            "last_replayed_as_of_date" if dry_run else "last_target_as_of_date"
        )
        previous_as_of_date = state.get(cycle_state_key)
        if previous_as_of_date:
            try:
                previous_as_of = pd.to_datetime(
                    str(previous_as_of_date), format="%Y%m%d", errors="raise"
                )
                basket_as_of = pd.to_datetime(
                    basket_as_of_date, format="%Y%m%d", errors="raise"
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"V7.13 persisted monthly cycle is invalid: "
                    f"{previous_as_of_date!r}"
                ) from exc
            if basket_as_of <= previous_as_of:
                reason = (
                    "monthly_cycle_already_consumed"
                    if basket_as_of == previous_as_of
                    else "monthly_cycle_rollback_rejected"
                )
                next_state = dict(state)
                next_state.update({
                    "last_ignored_basket_sha256": basket_id,
                    "last_ignored_allocation_sha256": allocation_id,
                    "last_ignored_decision_date": decision_date,
                    "last_ignored_as_of_date": basket_as_of_date,
                    "last_ignored_reason": reason,
                })
                ctx.set_strategy_state(next_state)
                log = logger.warning if basket_as_of == previous_as_of else logger.error
                log(
                    "V7.13[%s] ignored artifact basket=%s decision=%s as_of=%s "
                    "previous_as_of=%s reason=%s",
                    ctx.instance_id, basket_id[:12], decision_date,
                    basket_as_of_date, previous_as_of_date, reason,
                )
                return []

        if target_age_days > max_age_days:
            raise ValueError(
                f"V7.13 target is stale by {target_age_days} days "
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
                "last_replayed_as_of_date": basket_as_of_date,
                "last_replayed_allocation_sha256": allocation_id,
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
            "last_target_as_of_date": basket_as_of_date,
            "last_target_allocation_sha256": allocation_id,
            # 共享 QMT 账户无法把重叠 ETF 的真实持仓直接切成单策略快照；
            # V7.13 依靠 order_signal_map + settlement 维护独立虚拟账本，
            # 物理账户由 portfolio 级总量对账兜底。
            "reconciliation_status": (
                "pending"
                if guard.get("reconciliation_scope") == "instance_qmt"
                else "attributed_ledger"
            ),
        })
        ctx.set_strategy_state(next_state)
        logger.info("V7.13[%s] emitted %d signals for basket=%s", ctx.instance_id, len(signals), basket_id[:12])
        return signals
