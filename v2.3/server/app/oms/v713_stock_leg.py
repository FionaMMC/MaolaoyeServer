"""V713 stock leg: derive a pure-stock target from the validated monthly V713 basket.

Design: docs/V713_STOCK_LEG_SYNTHETIC_DESIGN_20261023.md section 5. The stock leg holds
the basket's stock names, rescaled to the whole leg, in TOP50 and T1, and nothing in
AUX. It never recomputes TOP50, Hydra or the state signal.
"""
from __future__ import annotations

import copy

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
