"""Approved broker policy: reported day orders expire at China 15:00."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from live_client import gateway as gateway_module
from live_client.gateway import XtQMTGateway, live_order_remark
from test_live_client import _cfg, _orders, _qmt_order


CHINA = timezone(timedelta(hours=8))


def _observe(tmp_path, monkeypatch, *, observed_at, status=50, quantity=0,
             valid_date="20260909", broker_date="20260909", missing=False):
    gateway = XtQMTGateway(_cfg(tmp_path))
    gateway.trader = object()
    gateway.account = object()
    gateway.xtconstant = SimpleNamespace(
        STOCK_BUY=23, STOCK_SELL=24, ORDER_UNREPORTED=48,
        ORDER_WAIT_REPORTING=49, ORDER_REPORTED=50, ORDER_REPORTED_CANCEL=51,
        ORDER_PARTSUCC_CANCEL=52, ORDER_PART_CANCEL=53, ORDER_CANCELED=54,
        ORDER_PART_SUCC=55, ORDER_SUCCEEDED=56, ORDER_JUNK=57, ORDER_UNKNOWN=255,
    )
    row = {**_orders(valid_date)[1], "local_order_id": "17"}
    row["order_remark"] = live_order_remark(row)
    broker = _qmt_order(
        order_id=17, status=status, remark=row["order_remark"], traded_volume=quantity,
    )
    broker.traded_price = 4.01123456 if quantity else 0
    broker.order_time = int(datetime.strptime(
        broker_date + "091000", "%Y%m%d%H%M%S",
    ).replace(tzinfo=CHINA).timestamp())
    gateway._query_orders_for_settlement = lambda: [] if missing else [broker]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed_at.astimezone(tz) if tz else observed_at

    monkeypatch.setattr(gateway_module, "datetime", Clock)
    return gateway.settlement_observations([row])


@pytest.mark.parametrize("clock", ["15:00:00", "15:00:01"])
def test_reported_at_cutoff_is_policy_expired_without_waiting_for_qmt_junk(
    tmp_path, monkeypatch, clock,
):
    observed = datetime.fromisoformat(f"2026-09-09T{clock}+08:00")
    result = _observe(tmp_path, monkeypatch, observed_at=observed)
    assert result["pending_order_ids"] == []
    assert result["issues"] == []
    assert result["results"] == [{
        "order_id": "ho_1", "symbol": "510300.SH", "direction": "BUY",
        "filled_quantity": 0, "filled_price": 0.0,
        "status": "EXPIRED_BY_POLICY", "qmt_order_id": "17",
        "raw_qmt_status": 50, "status_observed_at": observed.isoformat(),
        "expiration_policy_id": "QMT_DAY_ORDER_1500_V1",
    }]


def test_reported_one_second_before_cutoff_remains_open(tmp_path, monkeypatch):
    result = _observe(tmp_path, monkeypatch, observed_at=datetime.fromisoformat(
        "2026-09-09T14:59:59+08:00",
    ))
    assert result["results"] == []
    assert result["pending_order_ids"] == ["ho_1"]


def test_policy_expiry_preserves_actual_partial_fill_and_raw_status(tmp_path, monkeypatch):
    result = _observe(tmp_path, monkeypatch, observed_at=datetime.fromisoformat(
        "2026-09-09T15:00:00+08:00",
    ), quantity=25)
    row = result["results"][0]
    assert result["pending_order_ids"] == []
    assert row["filled_quantity"] == 25
    assert row["filled_price"] == 4.01123456
    assert row["status"] == "EXPIRED_BY_POLICY"
    assert row["raw_qmt_status"] == 50


def test_full_fill_wins_over_reported_policy_expiry(tmp_path, monkeypatch):
    result = _observe(tmp_path, monkeypatch, observed_at=datetime.fromisoformat(
        "2026-09-09T15:00:00+08:00",
    ), quantity=100)
    assert result["results"][0]["status"] == "FILLED"
    assert result["results"][0]["filled_quantity"] == 100
    assert "expiration_policy_id" not in result["results"][0]


@pytest.mark.parametrize("status", [48, 49, 51, 52, 55, 255])
def test_other_open_statuses_are_not_reinterpreted_as_policy_expired(tmp_path, monkeypatch, status):
    result = _observe(tmp_path, monkeypatch, observed_at=datetime.fromisoformat(
        "2026-09-09T16:00:00+08:00",
    ), status=status, quantity=25)
    assert result["pending_order_ids"] == ["ho_1"]
    assert result["results"][0]["status"] == "PARTIAL"
    assert "expiration_policy_id" not in result["results"][0]


@pytest.mark.parametrize("status,expected", [(53, "CANCELLED"), (54, "CANCELLED"), (57, "REJECTED")])
def test_explicit_broker_terminal_statuses_keep_their_meaning(tmp_path, monkeypatch, status, expected):
    result = _observe(tmp_path, monkeypatch, observed_at=datetime.fromisoformat(
        "2026-09-09T16:00:00+08:00",
    ), status=status)
    assert result["results"][0]["status"] == expected
    assert "expiration_policy_id" not in result["results"][0]


def test_next_day_same_broker_id_cannot_expire_previous_day_batch(tmp_path, monkeypatch):
    result = _observe(tmp_path, monkeypatch, observed_at=datetime.fromisoformat(
        "2026-09-10T15:00:00+08:00",
    ), valid_date="20260909", broker_date="20260910")
    assert result["results"] == []
    assert result["pending_order_ids"] == ["ho_1"]


def test_full_broker_date_conflict_cannot_expire_current_batch(tmp_path, monkeypatch):
    result = _observe(tmp_path, monkeypatch, observed_at=datetime.fromisoformat(
        "2026-09-09T15:00:00+08:00",
    ), broker_date="20260908")
    assert result["results"] == []
    assert result["pending_order_ids"] == ["ho_1"]


def test_missing_order_is_not_invented_as_zero_fill_policy_expiry(tmp_path, monkeypatch):
    result = _observe(tmp_path, monkeypatch, observed_at=datetime.fromisoformat(
        "2026-09-09T15:00:00+08:00",
    ), missing=True)
    assert result["results"] == []
    assert result["pending_order_ids"] == ["ho_1"]


def test_pure_policy_normalizes_time_without_native_qmt_or_tzdata():
    from live_client.qmt_day_order_policy import day_order_expiration

    result = day_order_expiration(
        qmt_status=50, filled_quantity=0, ordered_quantity=100,
        valid_date="20260909", observed_at=datetime.fromisoformat("2026-09-09T07:00:00+00:00"),
        broker_order_time=91000,
    )
    assert result["status_observed_at"] == "2026-09-09T15:00:00+08:00"
    assert result["expiration_policy_id"] == "QMT_DAY_ORDER_1500_V1"


def test_pure_policy_rejects_naive_observation_time():
    from live_client.qmt_day_order_policy import day_order_expiration

    with pytest.raises(ValueError, match="时区"):
        day_order_expiration(
            qmt_status=50, filled_quantity=0, ordered_quantity=100,
            valid_date="20260909", observed_at=datetime(2026, 9, 9, 15),
        )


@pytest.mark.parametrize("broker_time", [
    0, 91000, 20260909, 20260909091000,
    int(datetime(2026, 9, 9, 9, 10, tzinfo=CHINA).timestamp()),
    int(datetime(2026, 9, 9, 9, 10, tzinfo=CHINA).timestamp()) * 1000,
])
def test_supported_broker_time_representations_remain_same_day_bound(broker_time):
    from live_client.qmt_day_order_policy import day_order_expiration

    result = day_order_expiration(
        qmt_status=50, filled_quantity=25, ordered_quantity=100,
        valid_date="20260909", observed_at=datetime(2026, 9, 9, 15, tzinfo=CHINA),
        broker_order_time=broker_time,
    )
    assert result["status"] == "EXPIRED_BY_POLICY"


@pytest.mark.parametrize("valid_date,broker_time", [
    ("", 91000), ("20260931", 91000), ("20260909", 20260931091000),
])
def test_missing_or_invalid_date_proof_disables_only_expiry_policy(valid_date, broker_time):
    from live_client.qmt_day_order_policy import day_order_expiration

    assert day_order_expiration(
        qmt_status=50, filled_quantity=25, ordered_quantity=100,
        valid_date=valid_date, observed_at=datetime(2026, 9, 9, 15, tzinfo=CHINA),
        broker_order_time=broker_time,
    ) is None
