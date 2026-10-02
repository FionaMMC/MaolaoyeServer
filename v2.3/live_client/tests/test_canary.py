"""Real MiniQMT canary safety gates; every broker call is faked."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from live_client import canary
from live_client.config import LiveClientConfig
from live_client.gateway import (
    AccountSnapshot,
    BrokerOrderSnapshot,
    MarketQuote,
    XtQMTGateway,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 1, 9, 45, tzinfo=SHANGHAI)


def _cfg(tmp_path: Path, **changes) -> LiveClientConfig:
    account_id = "LIVE_ACCOUNT_FOR_CANARY_TEST"
    values = {
        "mode": "live",
        "execution_domain": "live",
        "account_id": account_id,
        "expected_account_sha256": hashlib.sha256(account_id.encode()).hexdigest(),
        "account_alias": "hydra-live",
        "instance_id": "live_hydra",
        "api_key": "unused-for-direct-miniqmt-canary",
        "server_base_url": "https://server.invalid",
        "userdata_dir": tmp_path / "userdata",
        "session_id": 987654,
        "state_db": tmp_path / "state" / "live.db",
        "log_dir": tmp_path / "logs",
        "task_prefix": "HydraLiveCanaryTest",
        "trading_enabled": False,
        "allow_insecure_http": False,
        "allowed_symbols": frozenset({
            "510300.SH", "159915.SZ", "511260.SH", "518880.SH", "159981.SZ",
            "159985.SZ", "159930.SZ", "513500.SH", "513100.SH",
        }),
        "max_daily_orders": 1,
        "max_single_order_notional": 2_000,
        "max_daily_buy_notional": 2_000,
        "max_daily_sell_notional": 2_000,
        "max_daily_turnover_notional": 2_000,
        "max_price_offset_bps": 50,
    }
    values.update(changes)
    return LiveClientConfig(**values)


def _constants():
    return SimpleNamespace(
        ORDER_UNREPORTED=48,
        ORDER_WAIT_REPORTING=49,
        ORDER_REPORTED=50,
        ORDER_REPORTED_CANCEL=51,
        ORDER_PARTSUCC_CANCEL=52,
        ORDER_PART_CANCEL=53,
        ORDER_CANCELED=54,
        ORDER_PART_SUCC=55,
        ORDER_SUCCEEDED=56,
        ORDER_JUNK=57,
        ORDER_UNKNOWN=255,
    )


class FakeBroker:
    def __init__(self, cfg: LiveClientConfig):
        self.cfg = cfg
        self.xtconstant = _constants()
        self.orders: list[BrokerOrderSnapshot] = []
        self.quote = MarketQuote(
            symbol="510300.SH",
            last_price=4.0,
            bid1=3.999,
            ask1=4.0,
            source_time=NOW,
            captured_at=NOW,
            price_tick=0.001,
            up_limit=4.4,
            down_limit=3.6,
            is_trading=True,
            iopv=4.0,
        )

    def gateway(self, _cfg):
        return FakeGateway(self)


class FakeGateway:
    def __init__(self, broker: FakeBroker):
        self.broker = broker
        self.xtconstant = broker.xtconstant

    def connect(self):
        return None

    def close(self):
        return None

    def account_snapshot(self):
        return AccountSnapshot(
            account_id=self.broker.cfg.account_id,
            available_cash=10_000,
            total_asset=10_000,
            positions={},
            sellable_positions={},
        )

    def cancelable_orders(self):
        active = {
            self.xtconstant.ORDER_UNREPORTED,
            self.xtconstant.ORDER_WAIT_REPORTING,
            self.xtconstant.ORDER_REPORTED,
            self.xtconstant.ORDER_REPORTED_CANCEL,
            self.xtconstant.ORDER_PART_SUCC,
        }
        return [row for row in self.broker.orders if row.order_status in active]

    def canary_orders(self):
        return [row for row in self.broker.orders if row.strategy_name == "hydra_canary"]

    def orders_by_remark(self, remark):
        return [row for row in self.broker.orders if row.order_remark == remark]

    def market_quote(self, symbol):
        assert symbol == self.broker.quote.symbol
        return self.broker.quote

    def submit_canary(self, *, symbol, quantity, limit_price, remark):
        order = BrokerOrderSnapshot(
            account_id=self.broker.cfg.account_id,
            stock_code=symbol,
            order_id=10001,
            order_sysid="",
            order_time=94500,
            order_volume=quantity,
            price=limit_price,
            traded_volume=0,
            traded_price=0,
            order_status=self.xtconstant.ORDER_REPORTED,
            status_msg="已报",
            strategy_name="hydra_canary",
            order_remark=remark,
        )
        self.broker.orders.append(order)
        return order.order_id

    def query_order(self, order_id):
        return next((row for row in self.broker.orders if row.order_id == order_id), None)

    def cancel_order(self, order_id):
        for index, row in enumerate(self.broker.orders):
            if row.order_id == order_id:
                self.broker.orders[index] = replace(
                    row,
                    order_status=self.xtconstant.ORDER_CANCELED,
                    status_msg="已撤",
                )
                return
        raise AssertionError("missing fake order")


def _plan(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    broker = FakeBroker(cfg)
    monkeypatch.setenv(canary.CANARY_ENABLED_ENV, "false")
    path = cfg.log_dir / "canary" / "plan.json"
    payload = canary.plan_canary(
        cfg,
        symbol="510300.SH",
        output=path,
        gateway_factory=broker.gateway,
        now=NOW,
    )
    return cfg, broker, path, payload


def test_plan_is_read_only_hashed_private_and_hard_capped(tmp_path, monkeypatch):
    cfg, broker, path, payload = _plan(tmp_path, monkeypatch)

    assert broker.orders == []
    assert payload["scope"] == "REAL_MINIQMT_ONLY_NO_HYDRA_SERVER_LEDGER"
    assert payload["order"] == {
        "direction": "BUY",
        "symbol": "510300.SH",
        "quantity": 100,
        "limit_price": 4.0,
        "remark": payload["order"]["remark"],
    }
    assert payload["checks"]["notional_cny"] == 400
    unsigned = {key: value for key, value in payload.items() if key != "plan_sha256"}
    assert payload["plan_sha256"] == hashlib.sha256(
        json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if os.name == "nt":
        pytest.skip("POSIX 0600 mode bits are not an ACL assertion on Windows")
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert path.is_relative_to(cfg.log_dir)


def test_plan_fails_closed_on_missing_iopv_and_bad_evidence_path(
    tmp_path, monkeypatch,
):
    cfg = _cfg(tmp_path)
    broker = FakeBroker(cfg)
    broker.quote = replace(broker.quote, iopv=None)
    monkeypatch.setenv(canary.CANARY_ENABLED_ENV, "false")
    with pytest.raises(RuntimeError, match="--iopv"):
        canary.plan_canary(
            cfg,
            symbol="510300.SH",
            output=cfg.log_dir / "plan.json",
            gateway_factory=broker.gateway,
            now=NOW,
        )
    with pytest.raises(RuntimeError, match="log_dir"):
        canary.plan_canary(
            cfg,
            symbol="510300.SH",
            output=tmp_path / "outside.json",
            supplied_iopv=4.0,
            supplied_iopv_source="QMT UI",
            gateway_factory=broker.gateway,
            now=NOW,
        )


def test_submit_requires_independent_switch_hash_and_is_exactly_once(
    tmp_path, monkeypatch,
):
    cfg, broker, path, payload = _plan(tmp_path, monkeypatch)
    live_cfg = replace(cfg, trading_enabled=True)
    monkeypatch.setenv(canary.CANARY_ENABLED_ENV, "true")

    with pytest.raises(RuntimeError, match=canary.PLAN_CONFIRM_ENV):
        canary.submit_canary(
            live_cfg, plan_path=path, gateway_factory=broker.gateway, now=NOW,
        )
    assert broker.orders == []
    assert not Path(f"{path}.submit-lock.json").exists()

    monkeypatch.setenv(canary.PLAN_CONFIRM_ENV, payload["plan_sha256"])
    result = canary.submit_canary(
        live_cfg, plan_path=path, gateway_factory=broker.gateway, now=NOW,
    )
    assert result["status"] == "BROKER_ORDER_OBSERVED"
    assert result["broker_state"]["status"] == "ACTIVE_OR_UNKNOWN"
    assert [(row.stock_code, row.order_volume, row.price) for row in broker.orders] == [
        ("510300.SH", 100, 4.0),
    ]
    with pytest.raises(RuntimeError, match="submit lock"):
        canary.submit_canary(
            live_cfg, plan_path=path, gateway_factory=broker.gateway, now=NOW,
        )


def test_submit_requotes_and_refuses_price_drift_before_lock(tmp_path, monkeypatch):
    cfg, broker, path, payload = _plan(tmp_path, monkeypatch)
    live_cfg = replace(cfg, trading_enabled=True)
    broker.quote = replace(
        broker.quote, last_price=4.01, bid1=4.009, ask1=4.01, iopv=4.01,
    )
    monkeypatch.setenv(canary.CANARY_ENABLED_ENV, "true")
    monkeypatch.setenv(canary.PLAN_CONFIRM_ENV, payload["plan_sha256"])

    with pytest.raises(RuntimeError, match="漂移"):
        canary.submit_canary(
            live_cfg, plan_path=path, gateway_factory=broker.gateway, now=NOW,
        )
    assert broker.orders == []
    assert not Path(f"{path}.submit-lock.json").exists()


def test_status_and_cancel_require_exact_broker_identity_and_closed_trade_switch(
    tmp_path, monkeypatch,
):
    cfg, broker, path, payload = _plan(tmp_path, monkeypatch)
    live_cfg = replace(cfg, trading_enabled=True)
    monkeypatch.setenv(canary.CANARY_ENABLED_ENV, "true")
    monkeypatch.setenv(canary.PLAN_CONFIRM_ENV, payload["plan_sha256"])
    canary.submit_canary(
        live_cfg, plan_path=path, gateway_factory=broker.gateway, now=NOW,
    )

    status = canary.status_canary(
        cfg, plan_path=path, gateway_factory=broker.gateway, now=NOW,
    )
    assert status["status"] == "ACTIVE_OR_UNKNOWN"
    with pytest.raises(RuntimeError, match="TRADING_ENABLED"):
        canary.cancel_canary(
            live_cfg, plan_path=path, gateway_factory=broker.gateway, now=NOW,
        )

    monkeypatch.setenv(canary.CANCEL_CONFIRM_ENV, payload["plan_sha256"])
    cancelled = canary.cancel_canary(
        cfg, plan_path=path, gateway_factory=broker.gateway, now=NOW,
    )
    assert cancelled["cancel_sent"] is True
    final = canary.status_canary(
        cfg, plan_path=path, gateway_factory=broker.gateway, now=NOW,
    )
    assert final["status"] == "CANCELLED"
    events = [
        json.loads(line)
        for line in Path(f"{path}.events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert cfg.account_id not in Path(f"{path}.events.jsonl").read_text(encoding="utf-8")
    assert events[0]["previous_event_sha256"] == "0" * 64
    assert all(
        events[index]["previous_event_sha256"] == events[index - 1]["event_sha256"]
        for index in range(1, len(events))
    )


def test_plan_refuses_existing_orders_and_open_switches(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    broker = FakeBroker(cfg)
    monkeypatch.setenv(canary.CANARY_ENABLED_ENV, "true")
    with pytest.raises(RuntimeError, match="同时关闭"):
        canary.plan_canary(
            cfg,
            symbol="510300.SH",
            output=cfg.log_dir / "plan.json",
            gateway_factory=broker.gateway,
            now=NOW,
        )

    monkeypatch.setenv(canary.CANARY_ENABLED_ENV, "false")
    broker.orders.append(BrokerOrderSnapshot(
        account_id=cfg.account_id,
        stock_code="510300.SH",
        order_id=7,
        order_sysid="",
        order_time=94000,
        order_volume=100,
        price=4.0,
        traded_volume=100,
        traded_price=4.0,
        order_status=broker.xtconstant.ORDER_SUCCEEDED,
        status_msg="已成",
        strategy_name="hydra_canary",
        order_remark="HC|already",
    ))
    with pytest.raises(RuntimeError, match="已经存在"):
        canary.plan_canary(
            cfg,
            symbol="510300.SH",
            output=cfg.log_dir / "plan.json",
            gateway_factory=broker.gateway,
            now=NOW,
        )


def test_xt_gateway_canary_uses_exact_limit_order_contract(tmp_path):
    cfg = _cfg(tmp_path)
    constants = SimpleNamespace(STOCK_BUY=23, FIX_PRICE=11)
    raw_order = SimpleNamespace(
        account_id=cfg.account_id,
        stock_code="510300.SH",
        order_id=12345,
        order_sysid="sys-1",
        order_time=94500,
        order_volume=100,
        price=4.0,
        traded_volume=0,
        traded_price=0,
        order_status=50,
        status_msg="已报",
        strategy_name="hydra_canary",
        order_remark="HC|01234567890123456789",
    )

    class Trader:
        call = None

        def query_stock_orders(self, _account, _cancelable):
            return [raw_order]

        def query_stock_order(self, _account, order_id):
            return raw_order if order_id == 12345 else None

        def order_stock(self, *args):
            self.call = args
            return 12345

        def cancel_order_stock(self, _account, order_id):
            return 0 if order_id == 12345 else -1

    class ArrayLike:
        def __init__(self, values):
            self.values = values

        def __len__(self):
            return len(self.values)

        def __getitem__(self, index):
            return self.values[index]

        def __bool__(self):
            raise ValueError("array truth value is ambiguous")

    class Data:
        @staticmethod
        def get_full_tick(_symbols):
            return {"510300.SH": {
                "lastPrice": 4.0,
                "bidPrice": ArrayLike([3.999]),
                "askPrice": ArrayLike([4.0]),
                "time": int(NOW.timestamp() * 1000),
                "iopv": 4.0,
            }}

        @staticmethod
        def get_instrument_detail(_symbol):
            return {
                "PriceTick": 0.001,
                "UpStopPrice": 4.4,
                "DownStopPrice": 3.6,
                "IsTrading": True,
            }

    trader = Trader()
    gateway = XtQMTGateway(cfg)
    gateway.trader = trader
    gateway.account = object()
    gateway.xtconstant = constants
    gateway.xtdata = Data()

    quote = gateway.market_quote("510300.SH")
    assert (quote.bid1, quote.ask1, quote.iopv) == (3.999, 4.0, 4.0)
    order_id = gateway.submit_canary(
        symbol="510300.SH",
        quantity=100,
        limit_price=4.0,
        remark="HC|01234567890123456789",
    )
    assert order_id == 12345
    assert trader.call[1:] == (
        "510300.SH", 23, 100, 11, 4.0,
        "hydra_canary", "HC|01234567890123456789",
    )
    assert gateway.query_order(12345).order_sysid == "sys-1"
    assert gateway.cancelable_orders()[0].order_id == 12345
    assert gateway.canary_orders()[0].strategy_name == "hydra_canary"
    gateway.cancel_order(12345)
