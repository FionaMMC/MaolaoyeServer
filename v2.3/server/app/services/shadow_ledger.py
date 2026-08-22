"""Generic, order-ineligible shadow target ledger and daily mark-to-market."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import delete, select

from app.models import ShadowInstanceState, ShadowNavSnapshot, ShadowTarget
from app.storage.parquet import ParquetStore

logger = logging.getLogger(__name__)

TARGET_COLUMNS = {
    "shadow_id", "code", "weight", "decision_date", "as_of_date",
    "state_reason", "source_version", "input_hash",
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ)$")
_SIDECAR_COMMON_KEYS = {
    "shadow_id", "decision_date", "as_of_date", "source_version",
    "input_hash", "weight_sum",
}
_HYDRA_PAYLOAD_KEYS = {
    "shadow_id", "decision_date", "source_version", "input_hashes",
    "target_bond_abs_risk_budget", "trend_z", "duration_score",
    "signal_date", "training_label_end",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def shadow_target_hash(frame: pd.DataFrame) -> str:
    canonical = frame[sorted(TARGET_COLUMNS)].sort_values("code")
    payload = canonical.to_csv(
        index=False, lineterminator="\n", float_format="%.12g"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_shadow_sidecar(
    sidecar_path: Path, target: pd.DataFrame, constraints: dict | None = None,
) -> dict:
    """Validate producer provenance against an already-normalized target."""
    payload = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
    missing = sorted(_SIDECAR_COMMON_KEYS - set(payload))
    if missing:
        raise ValueError(f"sidecar is missing common provenance fields: {missing}")
    for field in ("shadow_id", "decision_date", "as_of_date", "source_version", "input_hash"):
        if str(payload[field]) != str(target[field].iloc[0]):
            raise ValueError(f"sidecar {field} does not match target")
    if not _HASH_RE.fullmatch(str(payload["input_hash"])):
        raise ValueError("sidecar input_hash must be a lowercase SHA-256")
    if abs(float(payload["weight_sum"]) - float(target["weight"].sum())) > 1e-8:
        raise ValueError("sidecar weight_sum does not match target")

    constraints = constraints or {}
    allowed_publishers = constraints.get("allowed_publisher_source_commits")
    is_hydra_payload = "input_hashes" in payload or allowed_publishers is not None
    if allowed_publishers is not None:
        if payload.get("publisher_source_commit") not in set(allowed_publishers):
            raise ValueError("sidecar publisher_source_commit is not approved")
    if is_hydra_payload:
        missing_hydra = sorted(_HYDRA_PAYLOAD_KEYS - set(payload))
        if missing_hydra:
            raise ValueError(
                f"sidecar is missing Hydra provenance fields: {missing_hydra}"
            )
        input_hashes = payload["input_hashes"]
        if (not isinstance(input_hashes, dict) or not input_hashes
                or not all(
                    _HASH_RE.fullmatch(str(value))
                    for value in input_hashes.values()
                )):
            raise ValueError(
                "sidecar input_hashes must contain lowercase SHA-256 values"
            )
        canonical = {key: payload[key] for key in _HYDRA_PAYLOAD_KEYS}
        expected_hash = hashlib.sha256(
            json.dumps(
                canonical, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if expected_hash != target["input_hash"].iloc[0]:
            raise ValueError(
                "sidecar provenance hash does not match target input_hash"
            )
    return payload


class ShadowBoundaryError(ValueError):
    """A configuration or target attempted to cross the no-order boundary."""


class ShadowLedgerService:
    """Consume versioned targets and maintain theoretical cash/positions/NAV.

    This module intentionally imports no order, trade, signal, or settlement model.
    """

    def __init__(self, session_factory, parquet_store: ParquetStore, config_path: Path):
        self.session_factory = session_factory
        self.store = parquet_store
        self.config_path = Path(config_path)

    def load_instances(self) -> list[dict]:
        if not self.config_path.exists():
            return []
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        instances = []
        seen = set()
        for raw in cfg.get("shadow_instances", []):
            item = dict(raw)
            shadow_id = str(item.get("shadow_id", "")).strip()
            if not shadow_id or shadow_id in seen:
                raise ShadowBoundaryError("shadow_id must be non-empty and unique")
            seen.add(shadow_id)
            if item.get("qmt_account_id") not in (None, ""):
                raise ShadowBoundaryError(f"{shadow_id} must not bind a QMT account")
            if item.get("orders_enabled", False):
                raise ShadowBoundaryError(f"{shadow_id} must not enable orders")
            if item.get("mode", "shadow") != "shadow":
                raise ShadowBoundaryError(f"{shadow_id} must use mode=shadow")
            target_file = Path(str(item.get("target_file", "")))
            if not target_file.is_absolute():
                target_file = self.config_path.parent / target_file
            allowed_symbols_raw = item.get("allowed_symbols")
            allowed_symbols = None
            if allowed_symbols_raw is not None:
                allowed_symbols = [str(value).strip() for value in allowed_symbols_raw]
                if (not allowed_symbols
                        or len(allowed_symbols) != len(set(allowed_symbols))
                        or not all(_CODE_RE.fullmatch(value) for value in allowed_symbols)):
                    raise ShadowBoundaryError(
                        f"{shadow_id} allowed_symbols must be unique tradeable codes"
                    )
            fallback_reasons_raw = item.get("allowed_symbol_fallback_state_reasons")
            fallback_reasons = None
            if fallback_reasons_raw is not None:
                fallback_reasons = [
                    str(value).strip() for value in fallback_reasons_raw
                ]
                if (allowed_symbols is None
                        or not fallback_reasons
                        or len(fallback_reasons) != len(set(fallback_reasons))
                        or any(not value for value in fallback_reasons)):
                    raise ShadowBoundaryError(
                        f"{shadow_id} symbol fallback reasons require an allowlist "
                        "and must be unique and non-empty"
                    )
            required_symbols_raw = item.get("required_symbols")
            required_symbols = None
            if required_symbols_raw is not None:
                required_symbols = [str(value).strip() for value in required_symbols_raw]
                if (not required_symbols
                        or len(required_symbols) != len(set(required_symbols))
                        or not all(_CODE_RE.fullmatch(value) for value in required_symbols)):
                    raise ShadowBoundaryError(
                        f"{shadow_id} required_symbols must be unique tradeable codes"
                    )
                if (allowed_symbols is not None
                        and not set(required_symbols).issubset(allowed_symbols)):
                    raise ShadowBoundaryError(
                        f"{shadow_id} required_symbols must be inside allowed_symbols"
                    )
            allowed_source_versions_raw = item.get("allowed_source_versions")
            allowed_source_versions = None
            if allowed_source_versions_raw is not None:
                allowed_source_versions = [
                    str(value).strip() for value in allowed_source_versions_raw
                ]
                if (not allowed_source_versions
                        or len(allowed_source_versions) != len(set(allowed_source_versions))
                        or any(not value for value in allowed_source_versions)):
                    raise ShadowBoundaryError(
                        f"{shadow_id} allowed_source_versions must be unique and non-empty"
                    )
            allowed_publishers_raw = item.get("allowed_publisher_source_commits")
            allowed_publisher_source_commits = None
            if allowed_publishers_raw is not None:
                allowed_publisher_source_commits = [
                    str(value).strip() for value in allowed_publishers_raw
                ]
                if (not allowed_publisher_source_commits
                        or len(allowed_publisher_source_commits)
                        != len(set(allowed_publisher_source_commits))
                        or not all(
                            re.fullmatch(r"[0-9a-f]{40}", value)
                            for value in allowed_publisher_source_commits
                        )):
                    raise ShadowBoundaryError(
                        f"{shadow_id} publisher commits must be unique full Git SHAs"
                    )
            max_target_age_days = int(item.get("max_target_age_days", 45))
            if max_target_age_days < 0:
                raise ShadowBoundaryError(
                    f"{shadow_id} max_target_age_days must be non-negative"
                )
            instances.append({
                "shadow_id": shadow_id,
                "target_file": target_file,
                "initial_cash": float(item.get("initial_cash", 10_000_000)),
                "commission_rate": float(item.get("commission_rate", 0.0003)),
                "min_commission": float(item.get("min_commission", 5.0)),
                "stamp_duty_sell": float(item.get("stamp_duty_sell", 0.0005)),
                "lot_size": int(item.get("lot_size", 100)),
                "max_price_staleness_days": int(item.get("max_price_staleness_days", 7)),
                "max_target_age_days": max_target_age_days,
                "allowed_symbols": allowed_symbols,
                "allowed_symbol_fallback_state_reasons": fallback_reasons,
                "required_symbols": required_symbols,
                "allowed_source_versions": allowed_source_versions,
                "allowed_publisher_source_commits": allowed_publisher_source_commits,
                "require_sidecar": bool(item.get("require_sidecar", False)),
                "enabled": bool(item.get("enabled", True)),
            })
        return instances

    def validate_target(
        self, frame: pd.DataFrame, shadow_id: str, trade_date: int,
        constraints: dict | None = None,
    ) -> tuple[pd.DataFrame, str]:
        if set(frame.columns) != TARGET_COLUMNS:
            missing = sorted(TARGET_COLUMNS - set(frame.columns))
            extra = sorted(set(frame.columns) - TARGET_COLUMNS)
            raise ValueError(f"shadow target schema mismatch missing={missing} extra={extra}")
        if frame.empty:
            raise ValueError("shadow target must not be empty")
        data = frame.copy()
        for column in TARGET_COLUMNS - {"weight"}:
            data[column] = data[column].astype(str).str.strip()
        if data["shadow_id"].drop_duplicates().tolist() != [shadow_id]:
            raise ValueError("shadow target shadow_id does not match its instance")
        if data["code"].duplicated().any():
            raise ValueError("shadow target contains duplicate codes")
        if not data["code"].map(lambda value: bool(_CODE_RE.fullmatch(value))).all():
            raise ValueError("shadow target contains a non-tradeable code")
        data["weight"] = pd.to_numeric(data["weight"], errors="coerce")
        if (data["weight"].isna().any()
                or not data["weight"].map(math.isfinite).all()
                or (data["weight"] <= 0).any()):
            raise ValueError("shadow target weights must be finite and positive")
        if abs(float(data["weight"].sum()) - 1.0) > 1e-6:
            raise ValueError("shadow target weights must sum to 1")
        for column in (
            "decision_date", "as_of_date", "state_reason", "source_version", "input_hash"
        ):
            if data[column].nunique(dropna=False) != 1 or not data[column].iloc[0]:
                raise ValueError(f"shadow target must have one non-empty {column}")
        if not _HASH_RE.fullmatch(data["input_hash"].iloc[0]):
            raise ValueError("shadow target input_hash must be a lowercase SHA-256")
        decision = pd.to_datetime(data["decision_date"].iloc[0], format="%Y%m%d")
        as_of = pd.to_datetime(data["as_of_date"].iloc[0], format="%Y%m%d")
        run_date = pd.to_datetime(str(trade_date), format="%Y%m%d")
        if as_of > decision:
            raise ValueError("shadow target as_of_date is after decision_date")
        if decision > run_date:
            raise ValueError("shadow target decision_date is in the future")
        constraints = constraints or {}
        max_target_age_days = constraints.get("max_target_age_days")
        if (max_target_age_days is not None
                and (run_date - as_of).days > int(max_target_age_days)):
            raise ValueError(
                f"shadow target is stale: as_of_date={as_of:%Y%m%d} "
                f"max_age_days={int(max_target_age_days)}"
            )
        allowed_symbols = constraints.get("allowed_symbols")
        if allowed_symbols is not None:
            unexpected = sorted(set(data["code"]) - set(allowed_symbols))
            fallback_reasons = set(
                constraints.get("allowed_symbol_fallback_state_reasons") or []
            )
            is_approved_fallback = data["state_reason"].iloc[0] in fallback_reasons
            if unexpected and not is_approved_fallback:
                raise ValueError(
                    f"shadow target contains symbols outside its allowlist: {unexpected}"
                )
        required_symbols = constraints.get("required_symbols")
        if required_symbols is not None:
            missing_required = sorted(set(required_symbols) - set(data["code"]))
            if missing_required:
                raise ValueError(
                    f"shadow target is missing required symbols: {missing_required}"
                )
        allowed_source_versions = constraints.get("allowed_source_versions")
        if (allowed_source_versions is not None
                and data["source_version"].iloc[0] not in set(allowed_source_versions)):
            raise ValueError(
                "shadow target source_version is not approved for this instance"
            )
        return data, shadow_target_hash(data)

    def run_all(self, trade_date: int) -> dict:
        summaries = []
        for cfg in self.load_instances():
            if not cfg["enabled"]:
                summaries.append({"shadow_id": cfg["shadow_id"], "status": "disabled"})
                continue
            try:
                summaries.append(self._run_one(cfg, trade_date))
            except Exception as exc:
                logger.exception("shadow ledger failed id=%s: %s", cfg["shadow_id"], exc)
                self._mark_failed(cfg, trade_date, str(exc))
                summaries.append({
                    "shadow_id": cfg["shadow_id"], "status": "blocked", "reason": str(exc)
                })
        return {"trade_date": trade_date, "instances": summaries}

    def _run_one(self, cfg: dict, trade_date: int) -> dict:
        path = cfg["target_file"]
        if not path.exists():
            raise FileNotFoundError(f"target missing: {path}")
        frame = pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)
        target, target_hash = self.validate_target(
            frame, cfg["shadow_id"], trade_date, constraints=cfg
        )
        if cfg["require_sidecar"]:
            sidecar_path = path.with_suffix(".json")
            if not sidecar_path.is_file():
                raise FileNotFoundError(
                    f"required producer sidecar missing: {sidecar_path}"
                )
            validate_shadow_sidecar(sidecar_path, target, cfg)
        prices = self._prices(
            set(target["code"].tolist()), trade_date, cfg["max_price_staleness_days"]
        )

        with self.session_factory() as session:
            state = session.get(ShadowInstanceState, cfg["shadow_id"])
            if state is None:
                state = ShadowInstanceState(
                    shadow_id=cfg["shadow_id"], initial_cash=cfg["initial_cash"],
                    virtual_cash=cfg["initial_cash"], virtual_positions={},
                    status="initialized", last_update=_now_iso(),
                )
                session.add(state)
                session.flush()

            existing_codes = set((state.virtual_positions or {}).keys())
            if existing_codes - set(prices):
                prices.update(self._prices(
                    existing_codes - set(prices), trade_date,
                    cfg["max_price_staleness_days"],
                ))

            transaction_cost = 0.0
            turnover = 0.0
            if state.target_hash != target_hash:
                transaction_cost, turnover = self._rebalance(state, target, prices, cfg)
                session.execute(
                    delete(ShadowTarget).where(
                        ShadowTarget.shadow_id == cfg["shadow_id"],
                        ShadowTarget.decision_date == target["decision_date"].iloc[0],
                    )
                )
                now = _now_iso()
                for row in target.to_dict("records"):
                    session.add(ShadowTarget(
                        **row, target_hash=target_hash, created_at=now,
                    ))

            state.status = "active"
            state.decision_date = target["decision_date"].iloc[0]
            state.as_of_date = target["as_of_date"].iloc[0]
            state.state_reason = target["state_reason"].iloc[0]
            state.source_version = target["source_version"].iloc[0]
            state.input_hash = target["input_hash"].iloc[0]
            state.target_hash = target_hash
            state.cumulative_cost = float(state.cumulative_cost) + transaction_cost
            state.last_turnover = turnover
            state.last_update = _now_iso()
            nav = self._nav(state.virtual_cash, state.virtual_positions, prices)
            self._upsert_snapshot(session, state, trade_date, nav, transaction_cost, turnover)
            session.commit()

        return {
            "shadow_id": cfg["shadow_id"], "status": "active", "nav": nav,
            "turnover": turnover, "transaction_cost": transaction_cost,
            "target_hash": target_hash,
        }

    def _prices(
        self, symbols: set[str], trade_date: int, max_staleness_days: int
    ) -> dict[str, float]:
        run_date = pd.to_datetime(str(trade_date), format="%Y%m%d")
        prices = {}
        for symbol in sorted(symbols):
            found = None
            for category in ("stocks", "etfs"):
                data = self.store.read(category, symbol, end_date=trade_date)
                if not data.empty:
                    found = data.iloc[-1]
                    break
            if found is None:
                raise ValueError(f"no price on or before {trade_date} for {symbol}")
            price_date = pd.to_datetime(str(int(found["trade_date"])), format="%Y%m%d")
            if (run_date - price_date).days > max_staleness_days:
                raise ValueError(f"stale price for {symbol}: {int(found['trade_date'])}")
            price = float(found["close"])
            if not math.isfinite(price) or price <= 0:
                raise ValueError(f"invalid close for {symbol}")
            prices[symbol] = price
        return prices

    @staticmethod
    def _fees(gross: float, direction: str, cfg: dict) -> float:
        commission = max(cfg["min_commission"], gross * cfg["commission_rate"])
        duty = gross * cfg["stamp_duty_sell"] if direction == "SELL" else 0.0
        return commission + duty

    def _rebalance(
        self, state: ShadowInstanceState, target: pd.DataFrame,
        prices: dict[str, float], cfg: dict,
    ) -> tuple[float, float]:
        old = {code: int(qty) for code, qty in (state.virtual_positions or {}).items()}
        nav_before = self._nav(state.virtual_cash, old, prices)
        lot = cfg["lot_size"]
        desired = {
            row.code: int((nav_before * float(row.weight)) / prices[row.code] / lot) * lot
            for row in target.itertuples(index=False)
        }
        cash = float(state.virtual_cash)
        cost = 0.0
        traded_value = 0.0

        # Sells always settle before buys in the theoretical ledger as well.
        positions = dict(old)
        for code in sorted(set(old) | set(desired)):
            qty = max(0, old.get(code, 0) - desired.get(code, 0))
            if qty:
                gross = qty * prices[code]
                fee = self._fees(gross, "SELL", cfg)
                cash += gross - fee
                cost += fee
                traded_value += gross
                positions[code] = old.get(code, 0) - qty
                if positions[code] <= 0:
                    positions.pop(code, None)

        for code in sorted(desired):
            qty = max(0, desired[code] - positions.get(code, 0))
            while qty > 0:
                gross = qty * prices[code]
                fee = self._fees(gross, "BUY", cfg)
                if gross + fee <= cash + 1e-9:
                    break
                qty -= lot
            if qty:
                gross = qty * prices[code]
                fee = self._fees(gross, "BUY", cfg)
                cash -= gross + fee
                cost += fee
                traded_value += gross
                positions[code] = positions.get(code, 0) + qty

        state.virtual_cash = round(cash, 6)
        state.virtual_positions = {k: v for k, v in positions.items() if v > 0}
        turnover = traded_value / (2.0 * nav_before) if nav_before > 0 else 0.0
        return round(cost, 6), round(turnover, 10)

    @staticmethod
    def _nav(cash: float, positions: dict, prices: dict[str, float]) -> float:
        return round(float(cash) + sum(int(q) * prices[s] for s, q in positions.items()), 6)

    def _upsert_snapshot(
        self, session, state: ShadowInstanceState, trade_date: int, nav: float,
        transaction_cost: float, turnover: float,
    ) -> None:
        date_str = str(trade_date)
        previous = session.execute(
            select(ShadowNavSnapshot)
            .where(ShadowNavSnapshot.shadow_id == state.shadow_id)
            .where(ShadowNavSnapshot.date < date_str)
            .order_by(ShadowNavSnapshot.date.desc())
            .limit(1)
        ).scalar_one_or_none()
        daily_return = None if previous is None or previous.nav == 0 else nav / previous.nav - 1.0
        values = {
            "nav": nav, "daily_return": daily_return,
            "virtual_cash": state.virtual_cash,
            "positions_snapshot": dict(state.virtual_positions or {}),
            "transaction_cost": transaction_cost, "turnover": turnover,
            "decision_date": state.decision_date, "as_of_date": state.as_of_date,
            "state_reason": state.state_reason, "source_version": state.source_version,
            "input_hash": state.input_hash, "target_hash": state.target_hash,
            "created_at": _now_iso(),
        }
        row = session.get(ShadowNavSnapshot, (state.shadow_id, date_str))
        if row is None:
            session.add(ShadowNavSnapshot(shadow_id=state.shadow_id, date=date_str, **values))
        else:
            # Scheduler retry on the same date must not erase the first run's
            # rebalance cost/turnover.  A changed target on the same date adds to it.
            values["transaction_cost"] += float(row.transaction_cost)
            values["turnover"] += float(row.turnover)
            for key, value in values.items():
                setattr(row, key, value)

    def _mark_failed(self, cfg: dict, trade_date: int, reason: str) -> None:
        with self.session_factory() as session:
            state = session.get(ShadowInstanceState, cfg["shadow_id"])
            if state is None:
                state = ShadowInstanceState(
                    shadow_id=cfg["shadow_id"], initial_cash=cfg["initial_cash"],
                    virtual_cash=cfg["initial_cash"], virtual_positions={},
                    status="blocked", last_update=_now_iso(),
                )
                session.add(state)
            state.status = "blocked"
            state.state_reason = reason[:1000]
            state.last_update = _now_iso()
            session.commit()
