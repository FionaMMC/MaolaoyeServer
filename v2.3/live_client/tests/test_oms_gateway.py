"""OMS gateway surface: submit_limit, day_orders, day_trades, quotes.

The xtquant SDK is never imported: a fake trader is injected into an
unconnected XtQMTGateway, as in test_live_client.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from live_client.gateway import BrokerOrderSnapshot, MockQMTGateway, XtQMTGateway

CST = timezone(timedelta(hours=8))
CONSTANTS = SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11)
ACCOUNT = SimpleNamespace(account_id="LIVE_ACCOUNT_FOR_TEST")


def _gateway(trader, xtdata=None) -> XtQMTGateway:
    gateway = object.__new__(XtQMTGateway)
    gateway.cfg = None
    gateway.trader = trader
    gateway.account = ACCOUNT
    gateway.xtconstant = CONSTANTS
    gateway.xtdata = xtdata
    return gateway


class RecordingTrader:
    def __init__(self, result=12345, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def order_stock(self, *args):
        self.calls.append(args)
        if self.raises is not None:
            raise self.raises
        return self.result


def test_submit_limit_positive_id_is_submitted_and_remark_is_passed_unchanged():
    trader = RecordingTrader(result=12345)
    gateway = _gateway(trader)

    sell = gateway.submit_limit(symbol="510300.SH", side="SELL", quantity=1500,
                                limit_price=4.598, remark="H000010101")
    remark = "H000010201|this-is-longer-than-24-chars"
    buy = gateway.submit_limit(symbol="513100.SH", side="BUY", quantity=600,
                               limit_price=2.228, remark=remark)

    assert (sell.status, sell.local_order_id) == ("SUBMITTED", "12345")
    assert sell.execution_meta["qmt_order_id"] == "12345"
    assert buy.status == "SUBMITTED"
    assert trader.calls == [
        (ACCOUNT, "510300.SH", 24, 1500, 11, 4.598, "hydra_oms", "H000010101"),
        (ACCOUNT, "513100.SH", 23, 600, 11, 2.228, "hydra_oms", remark),
    ]


@pytest.mark.parametrize("returned,local_id", [(-1, "-1"), (None, None)])
def test_submit_limit_negative_or_none_is_rejected(returned, local_id):
    result = _gateway(RecordingTrader(result=returned)).submit_limit(
        symbol="510300.SH", side="BUY", quantity=100, limit_price=4.6, remark="H000010201")
    assert result.status == "REJECTED"
    assert result.local_order_id == local_id


def test_submit_limit_zero_id_is_not_trusted_as_either_outcome():
    result = _gateway(RecordingTrader(result=0)).submit_limit(
        symbol="510300.SH", side="BUY", quantity=100, limit_price=4.6, remark="H000010201")
    assert result.status == "UNKNOWN"
    assert result.local_order_id is None


def test_submit_limit_exception_is_unknown_never_rejected():
    trader = RecordingTrader(raises=TimeoutError("response lost"))
    result = _gateway(trader).submit_limit(
        symbol="510300.SH", side="BUY", quantity=100, limit_price=4.6, remark="H000010201")
    assert result.status == "UNKNOWN"
    assert result.local_order_id is None
    assert "TimeoutError" in result.detail
    assert len(trader.calls) == 1


def test_submit_limit_validates_before_touching_the_broker():
    trader = RecordingTrader()
    gateway = _gateway(trader)
    with pytest.raises(ValueError):
        gateway.submit_limit(symbol="510300.SH", side="HOLD", quantity=100,
                             limit_price=4.6, remark="H000010201")
    with pytest.raises(ValueError):
        gateway.submit_limit(symbol="510300.SH", side="BUY", quantity=0,
                             limit_price=4.6, remark="H000010201")
    with pytest.raises(ValueError):
        gateway.submit_limit(symbol="510300.SH", side="BUY", quantity=100,
                             limit_price=0.0, remark="H000010201")
    assert trader.calls == []
    gateway.trader = None
    with pytest.raises(RuntimeError, match="尚未连接"):
        gateway.submit_limit(symbol="510300.SH", side="BUY", quantity=100,
                             limit_price=4.6, remark="H000010201")


def _raw_order(order_id, status, remark, traded=0):
    return SimpleNamespace(
        account_id="LIVE_ACCOUNT_FOR_TEST", stock_code="510300.SH", order_id=order_id,
        order_sysid=f"sys-{order_id}", order_time=1791442625, order_volume=1500, price=4.598,
        traded_volume=traded, traded_price=4.61 if traded else 0.0, order_status=status,
        status_msg="", strategy_name="hydra_oms", order_remark=remark, order_type=24)


def test_day_orders_reads_all_current_day_orders_through_the_async_query():
    class Trader:
        def query_stock_orders_async(self, account, callback):
            assert account is ACCOUNT
            callback([_raw_order(1, 50, "H000010101"), _raw_order(2, 56, "H000010102", 1500)])

        def query_stock_orders(self, *_args):
            raise AssertionError("day_orders must use the bounded async query")

    orders = _gateway(Trader()).day_orders()
    assert all(isinstance(o, BrokerOrderSnapshot) for o in orders)
    assert [(o.order_id, o.order_status, o.order_remark, o.traded_volume) for o in orders] == [
        (1, 50, "H000010101", 0), (2, 56, "H000010102", 1500)]


def test_day_trades_maps_trade_details_to_wire_shape():
    traded_at = datetime(2026, 10, 8, 15, 0, 0, tzinfo=CST)

    class Trader:
        def query_stock_trades_async(self, account, callback):
            assert account is ACCOUNT
            callback([
                SimpleNamespace(traded_id="T1", order_id=1, stock_code="510300.SH", order_type=24,
                                traded_volume=1500, traded_price=4.611,
                                traded_time=int(traded_at.timestamp()), order_remark="H000010101"),
                SimpleNamespace(traded_id="T2", order_id=9, stock_code="513100.SH", order_type=23,
                                traded_volume=300, traded_price=2.2, traded_time=93000,
                                order_remark=None),
            ])

    trades = _gateway(Trader()).day_trades(timeout_seconds=1)
    assert trades == [
        {"broker_trade_id": "T1", "broker_order_id": "1", "symbol": "510300.SH", "side": "SELL",
         "quantity": 1500, "price": 4.611, "traded_at": traded_at.isoformat(), "remark": "H000010101"},
        {"broker_trade_id": "T2", "broker_order_id": "9", "symbol": "513100.SH", "side": "BUY",
         "quantity": 300, "price": 2.2, "traded_at": "93000", "remark": ""},
    ]


def test_day_trades_returns_none_when_qmt_never_answers():
    class Trader:
        def query_stock_trades_async(self, _account, _callback):
            return None  # MiniQMT accepted the request and never called back

        def query_stock_trades(self, *_args):
            raise AssertionError("must not fall back to the blocking query")

    assert _gateway(Trader()).day_trades(timeout_seconds=0.01) is None


def test_day_trades_refuses_an_unmappable_direction():
    class Trader:
        def query_stock_trades_async(self, _account, callback):
            callback([SimpleNamespace(traded_id="T1", order_id=1, stock_code="510300.SH",
                                      order_type=99, traded_volume=100, traded_price=4.6,
                                      traded_time=0, order_remark="")])

    with pytest.raises(RuntimeError, match="方向"):
        _gateway(Trader()).day_trades(timeout_seconds=1)


def test_quotes_reuse_market_quote_and_omit_symbols_without_a_valid_quote():
    now_ms = int(datetime(2026, 10, 8, 15, 0, 3, tzinfo=CST).timestamp() * 1000)

    class Data:
        @staticmethod
        def get_full_tick(symbols):
            ticks = {
                "510300.SH": {"lastPrice": 4.621, "bidPrice": [4.62], "askPrice": [4.622], "time": now_ms},
                "513100.SH": {"lastPrice": 2.196, "bidPrice": [0], "askPrice": [0], "time": now_ms},
            }
            return {s: ticks[s] for s in symbols if s in ticks}

        @staticmethod
        def get_instrument_detail(symbol):
            return {"PriceTick": 0.001, "UpStopPrice": 9.9, "DownStopPrice": 0.1,
                    "IsTrading": symbol == "510300.SH"}

    quotes = _gateway(RecordingTrader(), xtdata=Data()).quotes(
        ["510300.SH", "513100.SH", "159915.SZ"])
    assert quotes == {"510300.SH": {"last_price": 4.621, "is_trading": True}}


def _mock(tmp_path, **payload) -> MockQMTGateway:
    path = tmp_path / "mock.json"
    path.write_text(json.dumps({"account_id": "LIVE_ACCOUNT_FOR_TEST", **payload}), encoding="utf-8")
    return MockQMTGateway(path, "LIVE_ACCOUNT_FOR_TEST")


def test_mock_gateway_submit_limit_shows_up_in_day_orders(tmp_path):
    gateway = _mock(tmp_path, reject_symbols=["159915.SZ"], day_orders=[
        {"order_id": 7, "order_status": 56, "stock_code": "518880.SH", "order_remark": "manual"}])

    accepted = gateway.submit_limit(symbol="510300.SH", side="SELL", quantity=1500,
                                    limit_price=4.598, remark="H000010101")
    rejected = gateway.submit_limit(symbol="159915.SZ", side="BUY", quantity=100,
                                    limit_price=3.3, remark="H000010102")

    assert accepted.status == "SUBMITTED" and rejected.status == "REJECTED"
    orders = gateway.day_orders()
    assert [(o.order_id, o.order_status, o.order_remark) for o in orders] == [
        (7, 56, "manual"), (int(accepted.local_order_id), 50, "H000010101")]
    assert orders[1].order_type == 24 and orders[1].strategy_name == "hydra_oms"
    assert orders[1].order_volume == 1500 and orders[1].price == 4.598


def test_mock_gateway_trades_and_quotes_come_from_the_payload(tmp_path):
    trade = {"broker_trade_id": "T1", "broker_order_id": "7", "symbol": "518880.SH", "side": "BUY",
             "quantity": 100, "price": 9.1, "traded_at": "2026-10-08T09:25:00+08:00", "remark": "manual"}
    gateway = _mock(tmp_path, day_trades=[trade],
                    quotes={"510300.SH": {"last_price": 4.621, "is_trading": True}})
    assert gateway.day_trades() == [trade]
    assert gateway.quotes(["510300.SH", "513100.SH"]) == {
        "510300.SH": {"last_price": 4.621, "is_trading": True}}
    assert _mock(tmp_path, day_trades=[trade], trades_hang=True).day_trades() is None
