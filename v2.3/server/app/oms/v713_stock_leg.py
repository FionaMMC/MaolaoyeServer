"""V713 stock leg: derive a pure-stock target from the validated monthly V713 basket.

Design: docs/V713_STOCK_LEG_SYNTHETIC_DESIGN_20261023.md section 5. The stock leg holds
the basket's stock names, rescaled to the whole leg, in TOP50 and T1, and nothing in
AUX. It never recomputes TOP50, Hydra or the state signal.
"""
from __future__ import annotations

import copy
import hashlib
import json

STATE_STOCK_WEIGHT = {"TOP50": 1.0, "T1_5050": 0.5, "AUX_HYDRA": 0.0}
STOCK_LEG_TRANSFORM = "v713-stock-leg-v1"
_RULES = ("stock_board_lot", "stock_star", "etf")


def instrument_rule(symbol: str) -> str:
    """Frozen instrument rule for a symbol: 100-share board lots, STAR (688) or an ETF."""
    code, _, market = symbol.partition(".")
    if market == "SH" and code.startswith("688"):
        return "stock_star"
    if (market == "SH" and code.startswith("60")) or (market == "SZ" and code[:2] in ("00", "30")):
        return "stock_board_lot"
    if (market == "SH" and code.startswith("5")) or (market == "SZ" and code[:2] in ("15", "16")):
        return "etf"
    raise ValueError(f"no instrument rule for {symbol}")


def expand_policy(template: dict, symbols) -> dict:
    """Explicit V713 policy payload in which every symbol carries its rule's full spec."""
    if set(template.get("instrument_rules", {})) != set(_RULES):
        raise ValueError(f"policy template must define exactly {sorted(_RULES)}")
    payload = copy.deepcopy({key: value for key, value in template.items() if key != "instrument_rules"})
    payload["instruments"] = {symbol: dict(template["instrument_rules"][instrument_rule(symbol)])
                              for symbol in sorted(set(symbols))}
    return payload


def _sha(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def derive_stock_leg(payload: dict) -> dict:
    """Publication kwargs for the stock leg from ``app.oms.v713.build_publication`` output.

    TOP50 and T1 hold the basket's stock names rescaled to the whole leg; AUX is an
    explicit liquidation. The identity covers the transform, so a stock-leg target can
    never be mistaken for the mixed V713 allocation it came from.
    """
    from app.oms.planner import get_instrument

    policy, sleeve = payload["policy"], payload["provenance"]["sleeve"]
    if sleeve not in STATE_STOCK_WEIGHT:
        raise ValueError(f"unknown V713 sleeve {sleeve}")
    stocks = {symbol: float(weight) for symbol, weight in sorted(payload["weights"].items())
              if weight > 0 and get_instrument(symbol, policy).asset_type == "stock"}
    total = sum(stocks.values())
    if abs(total - STATE_STOCK_WEIGHT[sleeve]) > 1e-4:
        raise ValueError(f"{sleeve} basket carries stock weight {total:.6f}, expected {STATE_STOCK_WEIGHT[sleeve]}")
    liquidate = sleeve == "AUX_HYDRA"
    weights = {} if liquidate else {symbol: weight / total for symbol, weight in stocks.items()}
    identity = _sha({"transform": STOCK_LEG_TRANSFORM, "sleeve": sleeve, "signal_date": payload["signal_date"],
                     "weights": weights, "source_allocation_sha256": payload["source_sha256"]})
    provenance = {**payload["provenance"], "transform": STOCK_LEG_TRANSFORM,
                  "source_allocation_sha256": payload["source_sha256"],
                  "stock_leg_allocation_sha256": identity, "liquidate": liquidate}
    return {**payload, "weights": weights, "source_sha256": identity, "provenance": provenance,
            "liquidate": liquidate}


def record_state(session_factory, *, instance_id: str, payload: dict, target_version_id: str | None,
                 now: str) -> dict:
    """Record the month's V713 state for the synthetic ledger; idempotent and never rewritten."""
    from app.oms.cycles import CycleConflict
    from app.oms.models import V713StateRecord

    provenance = payload["provenance"]
    with session_factory() as session:
        row = session.get(V713StateRecord, (instance_id, payload["signal_date"]))
        if row is None:
            row = V713StateRecord(instance_id=instance_id, signal_date=payload["signal_date"],
                                  sleeve=provenance["sleeve"], decision_date=provenance["decision_date"],
                                  basket_sha256=provenance["basket_sha256"],
                                  allocation_sha256=payload["source_sha256"],
                                  target_version_id=target_version_id, recorded_at=now)
            session.add(row)
        elif (row.sleeve, row.allocation_sha256) != (provenance["sleeve"], payload["source_sha256"]):
            raise CycleConflict("V713 state for this month is already recorded with a different allocation")
        elif target_version_id is not None and row.target_version_id not in (None, target_version_id):
            raise CycleConflict("V713 state for this month already points to another target version")
        elif target_version_id is not None:
            row.target_version_id = target_version_id
        session.commit()
        return {"instance_id": row.instance_id, "signal_date": row.signal_date, "sleeve": row.sleeve,
                "target_version_id": row.target_version_id}
