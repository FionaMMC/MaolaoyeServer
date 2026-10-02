import pytest
from pydantic import ValidationError

from app.oms.schemas import EventIn, PlanOut, SnapshotIn


def _snapshot(**changes):
    body = dict(account_alias="hydra-live", kind="EOD", trade_date="20261008",
                taken_at="2026-10-08T15:05:00+08:00", available_cash=1000.0, total_asset=200000.0,
                positions={"510300.SH": 2500}, sellable={"510300.SH": 2500},
                orders=[dict(broker_order_id="1082138153", symbol="510300.SH", side="SELL", quantity=1000,
                             price=4.598, traded_volume=1000, traded_price=4.61, status=56, remark="H000010101")],
                trades=None, quotes={"510300.SH": {"last_price": 4.61}})
    body.update(changes)
    return body


def test_snapshot_accepts_valid_payload_and_defaults_trading():
    snap = SnapshotIn.model_validate(_snapshot())
    assert snap.quotes["510300.SH"].is_trading is True
    assert snap.trades is None


def test_snapshot_requires_zoned_time_and_date8():
    with pytest.raises(ValidationError):
        SnapshotIn.model_validate(_snapshot(taken_at="2026-10-08T15:05:00"))
    with pytest.raises(ValidationError):
        SnapshotIn.model_validate(_snapshot(trade_date="2026-10-08"))


def test_snapshot_rejects_unknown_kind_and_side():
    with pytest.raises(ValidationError):
        SnapshotIn.model_validate(_snapshot(kind="LATE"))
    bad = _snapshot()
    bad["orders"][0]["side"] = "SHORT"
    with pytest.raises(ValidationError):
        SnapshotIn.model_validate(bad)


def test_event_kind_is_closed_set():
    EventIn.model_validate(dict(event_id="e" * 32, client_order_id="H000010101", kind="ACKED",
                                observed_at="2026-10-08T14:57:06+08:00", broker_order_id="1"))
    with pytest.raises(ValidationError):
        EventIn.model_validate(dict(event_id="e" * 32, client_order_id="H000010101", kind="FILLED",
                                    observed_at="2026-10-08T14:57:06+08:00"))


def test_plan_out_round_trips():
    plan = PlanOut(account_alias="hydra-live", cycle_id="C00001", session_id="C00001:1", trade_date="20261008",
                   phase="SELL", executable=False, plan_sha256="0" * 64, frozen_target={"510300.SH": 1000},
                   orders=[dict(client_order_id="H000010101", symbol="510300.SH", side="SELL", quantity=1500,
                                limit_price=4.598)])
    assert PlanOut.model_validate(plan.model_dump(mode="json")) == plan
