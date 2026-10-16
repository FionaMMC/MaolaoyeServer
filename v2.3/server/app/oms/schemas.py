"""Wire format between the Windows agent and the server execution core."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _zoned(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("timestamp must carry a timezone")
    return value


class BrokerOrder(BaseModel):
    broker_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(ge=0)
    price: float = Field(ge=0)
    traded_volume: int = Field(ge=0)
    traded_price: float = Field(ge=0)
    status: int
    remark: str = ""
    status_msg: str = ""


class BrokerTrade(BaseModel):
    broker_trade_id: str
    broker_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    traded_at: str
    remark: str = ""


class Quote(BaseModel):
    last_price: float = Field(gt=0)
    is_trading: bool | None = None
    source_time: str | None = None
    market_status: str | None = None
    status_source: str | None = None
    price_source: str | None = None
    adjustment: str | None = None


class SnapshotIn(BaseModel):
    account_alias: str
    kind: Literal["PRE", "EOD", "ADHOC"]
    trade_date: str = Field(pattern=r"^\d{8}$")
    taken_at: datetime
    available_cash: float = Field(ge=0)
    frozen_cash: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    total_asset: float = Field(ge=0)
    positions: dict[str, int]
    sellable: dict[str, int]
    orders: list[BrokerOrder]
    # None = the broker trade query did not answer; order cumulative fills still apply.
    trades: list[BrokerTrade] | None = None
    quotes: dict[str, Quote] = Field(default_factory=dict)

    @field_validator("taken_at")
    @classmethod
    def _taken_at_zoned(cls, v: datetime) -> datetime:
        return _zoned(v)


class EventIn(BaseModel):
    event_id: str = Field(min_length=8)
    client_order_id: str
    kind: Literal["SUBMIT_STARTED", "ACKED", "SUBMIT_REJECTED", "SUBMIT_UNKNOWN", "CANCEL_REQUESTED"]
    observed_at: datetime
    broker_order_id: str | None = None
    detail: str | None = None

    @field_validator("observed_at")
    @classmethod
    def _observed_at_zoned(cls, v: datetime) -> datetime:
        return _zoned(v)


class EventsIn(BaseModel):
    account_alias: str
    events: list[EventIn]


class PlanOrder(BaseModel):
    client_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)


class PlanOut(BaseModel):
    schema_version: int = 1
    account_alias: str
    instance_id: str = ""
    cycle_id: str
    session_id: str
    trade_date: str
    phase: Literal["SELL", "BUY", "MANUAL"]
    executable: bool
    plan_sha256: str
    frozen_target: dict[str, int]
    orders: list[PlanOrder]
    policy: dict = Field(default_factory=dict)
    instrument_specs: dict = Field(default_factory=dict)
    allowed_symbols: list[str] = Field(default_factory=list)
    strategy_cash: float | None = None
    strategy_positions: dict[str, int] = Field(default_factory=dict)
    strategy_sellable: dict[str, int] = Field(default_factory=dict)
    server_reserved_order_ids: list[str] = Field(default_factory=list)
    server_accounted_fill_quantities: dict[str, int] = Field(default_factory=dict)
    server_terminal_orders: dict[str, dict] = Field(default_factory=dict)
    valid_until: str | None = None
    account_fingerprint: str = ""
    issued_at: str | None = None
    lease_expires_at: str | None = None


PLAN_HASH_FIELDS = (
    "schema_version", "account_alias", "instance_id", "cycle_id", "session_id", "trade_date", "phase",
    "frozen_target", "orders", "policy", "instrument_specs", "allowed_symbols", "valid_until", "account_fingerprint",
)


def plan_content_sha256(payload: dict) -> str:
    """Hash the immutable execution intent; resource balances and leases are refreshable."""
    import hashlib
    import json
    body = {key: payload[key] for key in PLAN_HASH_FIELDS}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()
