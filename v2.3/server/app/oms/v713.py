"""Turn an immutable, validated V7.13 basket into an OMS publication request.

This adapter never runs the legacy paper relay and never submits orders. It
requires a versioned, explicit execution policy: Hydra bands are not defaults for
an equity strategy. The caller supplies the account's attributed held symbols.
"""
from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from app.oms.instruments import InstrumentSpec
from app.oms.planner import Policy, get_instrument, policy_from_dict


CHINA = timezone(timedelta(hours=8))
_EXPLICIT_POLICY = {"policy_id", "strategy_id", "lot", "size_factor", "default_buy_bps", "buy_bps",
                    "sell_schedule", "window", "reserve", "instruments", "require_instrument_specs",
                    "execution_model", "execution_windows", "submit_at"}
# min_sell_quantity defaults to 1 and is checked for STAR symbols below.
_EXPLICIT_INSTRUMENT = {field.name for field in fields(InstrumentSpec)} - {"min_sell_quantity"}
_SYMBOL = re.compile(r"^\d{6}\.(SH|SZ)$")


def validate_v713_policy(payload: dict, symbols=()) -> Policy:
    if not isinstance(payload, dict):
        raise ValueError("V713 policy must be a JSON object")
    missing = _EXPLICIT_POLICY - set(payload)
    if missing:
        raise ValueError(f"V713 policy must explicitly specify {sorted(missing)}")
    if payload["strategy_id"] != "v713" or not str(payload["policy_id"]).startswith("V713_"):
        raise ValueError("V713 policy requires strategy_id=v713 and a versioned V713_ policy_id")
    if payload["require_instrument_specs"] is not True:
        raise ValueError("V713 requires explicit instrument specifications")
    for symbol, spec in payload["instruments"].items():
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise ValueError(f"unsupported V713 instrument symbol {symbol}")
        if not isinstance(spec, dict) or _EXPLICIT_INSTRUMENT - set(spec):
            raise ValueError(f"V713 must explicitly specify all instrument and fee rules for {symbol}")
        if symbol.startswith("688") and int(spec.get("min_sell_quantity", 1)) < 200:
            raise ValueError(f"STAR instrument {symbol} must set min_sell_quantity of at least 200")
    policy = policy_from_dict(payload)
    for symbol in symbols:
        get_instrument(symbol, policy)
    return policy


def _date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        raise ValueError("calendar dates must be YYYYMMDD strings")
    datetime.strptime(value, "%Y%m%d")
    return value


def build_publication(path: str | Path, *, closes: dict, calendar: list, policy_payload: dict,
                      now: str, held_symbols=()) -> dict:
    """Return kwargs for ``CycleService.publish_target`` (except account/instance).

    The exact bytes are read once and checked through the existing V713 contract
    validator in a private temporary directory. A concurrent replacement of the
    original publication cannot alter the validated payload or its provenance.
    """
    from plugins.v713_relay import V713RelayAdapter, allocation_hash

    path = Path(path)
    raw = path.read_bytes()
    raw_sha = hashlib.sha256(raw).hexdigest()
    with TemporaryDirectory(prefix="v713-oms-validate-") as directory:
        candidate = Path(directory) / "v713_target_latest.parquet"
        candidate.write_bytes(raw)

        class FrozenBasket(V713RelayAdapter):
            data_dir = Path(directory)

        frame = FrozenBasket()._read_latest_basket()
    if frame is None:
        raise ValueError("V713 basket is empty")

    as_of = _date(str(frame["as_of_date"].iloc[0]))
    decision = _date(str(frame["decision_date"].iloc[0]))
    days = sorted(set(_date(day) for day in calendar))
    if as_of not in days or decision not in days:
        raise ValueError("calendar must include the data as-of and decision sessions")
    prior_month = [day for day in days if day[:6] < decision[:6]]
    if not prior_month or as_of != prior_month[-1]:
        raise ValueError("V713 as_of_date must be the last completed month-end session")
    stamp = datetime.fromisoformat(now)
    if stamp.utcoffset() is None:
        raise ValueError("publication timestamp must carry a timezone")
    today = stamp.astimezone(CHINA).strftime("%Y%m%d")
    if as_of > today:
        raise ValueError("V713 data as_of_date is in the future")
    # A newer completed calendar month cannot silently reuse an older allocation.
    completed = [day for day in days if day[:6] < today[:6]]
    if completed and as_of < completed[-1]:
        raise ValueError("V713 basket is stale: a newer completed month is available")
    if not isinstance(closes, dict):
        raise ValueError("signal closes must be an object")
    weights = dict(zip(frame["code"].astype(str), frame["weight"].astype(float)))
    held = set(held_symbols)
    for symbol in held:
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError(f"invalid attributed held symbol {symbol}")
    # Identity remains the research allocation. The cycle owns the additional
    # zero targets for exits, so completing a sell cannot change publication ID.
    policy = validate_v713_policy(policy_payload, set(weights) | held)
    # Keep the executable monthly identity independent of publication dates.
    allocation_sha = allocation_hash(frame)
    return {"signal_date": as_of, "weights": weights, "signal_closes": dict(closes), "calendar": days,
            "source_sha256": allocation_sha, "now": now, "policy": policy,
            "earliest_execution_date": decision,
            "provenance": {"strategy_version": str(frame["strategy_version"].iloc[0]),
                           "sleeve": str(frame["sleeve"].iloc[0]), "as_of_date": as_of,
                           "decision_date": decision, "basket_sha256": str(frame["basket_sha256"].iloc[0]),
                           "allocation_sha256": allocation_sha, "file_sha256": raw_sha}}
