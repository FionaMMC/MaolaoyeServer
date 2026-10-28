"""Frozen instrument rules shared by planning, execution and settlement.

Rules are supplied by the approved publication contract. Symbol prefixes are not
used to infer board rules or taxes. The only implicit profile is the historical
Hydra ETF profile, retained for existing cycles and research parity.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from decimal import Decimal, ROUND_CEILING
import math
from typing import Mapping


HYDRA_ETFS = frozenset({"510300.SH", "159915.SZ", "511260.SH", "518880.SH", "159981.SZ",
                        "159985.SZ", "159930.SZ", "513500.SH", "513100.SH"})


@dataclass(frozen=True)
class InstrumentSpec:
    asset_type: str
    price_tick: float
    buy_lot: int = 100
    min_buy_quantity: int = 100
    sell_lot: int = 100
    odd_lot_sell_all: bool = True
    t_plus_one: bool = True
    commission_rate: float = .0001
    min_commission: float = 5.
    stamp_duty_sell: float = 0.
    transfer_fee_rate: float = 0.
    buy_reserve_rate: float | None = None
    # A partial sale below this is not a valid declaration (STAR: 200); a full exit always is.
    min_sell_quantity: int = 1

    def __post_init__(self):
        if self.asset_type not in ("stock", "etf"):
            raise ValueError("asset_type must be stock or etf")
        for name in ("price_tick", "commission_rate", "min_commission", "stamp_duty_sell", "transfer_fee_rate"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid instrument {name}")
        if self.price_tick <= 0 or self.price_tick > 1:
            raise ValueError("price_tick must be positive and no greater than one")
        for name in ("commission_rate", "stamp_duty_sell", "transfer_fee_rate"):
            if getattr(self, name) > 1:
                raise ValueError(f"invalid instrument {name}")
        for name in ("buy_lot", "min_buy_quantity", "sell_lot", "min_sell_quantity"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"invalid instrument {name}")
        if self.min_buy_quantity % self.buy_lot:
            raise ValueError("min_buy_quantity must be a multiple of buy_lot")
        if type(self.odd_lot_sell_all) is not bool or type(self.t_plus_one) is not bool:
            raise ValueError("instrument flags must be booleans")
        if self.buy_reserve_rate is not None:
            value = self.buy_reserve_rate
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("invalid buy_reserve_rate")
            if value < self.commission_rate:
                raise ValueError("buy_reserve_rate cannot be less than commission_rate")


def instrument_from_dict(payload: Mapping) -> InstrumentSpec:
    if not isinstance(payload, Mapping):
        raise ValueError("instrument must be an object")
    unknown = set(payload) - {f.name for f in fields(InstrumentSpec)}
    if unknown:
        raise ValueError(f"unknown instrument fields: {sorted(unknown)}")
    if not {"asset_type", "price_tick"} <= set(payload):
        raise ValueError("instrument requires asset_type and price_tick")
    return InstrumentSpec(**dict(payload))


def instrument_to_dict(spec: InstrumentSpec) -> dict:
    payload = asdict(spec)
    # Absent means 1: plans and frozen policies written before the field existed stay byte-identical.
    if payload["min_sell_quantity"] == 1:
        del payload["min_sell_quantity"]
    return payload


def hydra_instrument(symbol: str) -> InstrumentSpec:
    if symbol not in HYDRA_ETFS:
        raise ValueError(f"explicit instrument specification required for {symbol}")
    # Historical planner allowed arbitrary sell remainders and reserved 10 bp.
    return InstrumentSpec("etf", .001, sell_lot=1, buy_reserve_rate=.001)


def _money(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("money must be finite and nonnegative")
    return result


def estimate_fees(spec: InstrumentSpec, notional: float, side: str) -> float:
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    gross = _money(notional)
    if not gross:
        return 0.
    fees = max(_money(spec.min_commission), gross * _money(spec.commission_rate))
    fees += gross * _money(spec.transfer_fee_rate)
    if side == "SELL":
        fees += gross * _money(spec.stamp_duty_sell)
    return float(fees)


def buy_cost(spec: InstrumentSpec, price: float, quantity: int, *, reserve: bool = True) -> float:
    """Cash required, rounded up to cents; never under-reserve stated fees."""
    if type(quantity) is not int or quantity < 0:
        raise ValueError("quantity must be a nonnegative integer")
    if not quantity:
        return 0.
    notional = _money(price) * quantity
    rate = spec.buy_reserve_rate if reserve and spec.buy_reserve_rate is not None else spec.commission_rate
    fees = max(_money(spec.min_commission), notional * _money(rate))
    fees += notional * _money(spec.transfer_fee_rate)
    return float((notional + fees).quantize(Decimal(".01"), rounding=ROUND_CEILING))


def floor_buy_quantity(quantity: int | float, spec: InstrumentSpec) -> int:
    result = max(0, math.floor(quantity / spec.buy_lot) * spec.buy_lot)
    return result if result >= spec.min_buy_quantity else 0


def next_buy_quantity(quantity: int, spec: InstrumentSpec) -> int:
    return spec.min_buy_quantity if quantity == 0 else quantity + spec.buy_lot
