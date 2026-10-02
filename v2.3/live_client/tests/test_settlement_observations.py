"""One unresolved order does not discard independently verified broker facts."""
from types import SimpleNamespace

from live_client import cli
from live_client.core import validate_order_batch
from live_client.gateway import XtQMTGateway, live_order_remark
from live_client.state import LiveStateStore
from live_client.http_client import LiveServerClient
from live_client import http_client
from test_live_client import _cfg, _orders, _qmt_order


def _gateway(cfg, rows):
    gateway = XtQMTGateway(cfg)
    gateway.trader = object()
    gateway.account = object()
    gateway.xtconstant = SimpleNamespace(
        STOCK_BUY=23, STOCK_SELL=24, ORDER_UNREPORTED=48, ORDER_WAIT_REPORTING=49,
        ORDER_REPORTED=50, ORDER_REPORTED_CANCEL=51, ORDER_PARTSUCC_CANCEL=52,
        ORDER_PART_CANCEL=53, ORDER_CANCELED=54, ORDER_PART_SUCC=55,
        ORDER_SUCCEEDED=56, ORDER_JUNK=57, ORDER_UNKNOWN=255,
    )
    gateway._query_orders_for_settlement = lambda: rows
    gateway.connect = lambda: None
    gateway.close = lambda: None
    return gateway


def _submission(index, status, quantity):
    row = {**_orders()[1], "order_id": f"ho-{index}", "local_order_id": str(index)}
    row["order_remark"] = live_order_remark(row)
    broker = _qmt_order(
        order_id=index, status=status, remark=row["order_remark"], traded_volume=quantity,
    )
    return row, broker


def test_active_partial_and_terminal_facts_are_ingested_independently(tmp_path):
    pairs = [_submission(i, status, qty) for i, (status, qty) in enumerate(
        [(55, 25), (50, 0), (53, 50), (56, 100), (57, 0)], start=1,
    )]
    observations = _gateway(_cfg(tmp_path), [broker for _, broker in pairs]).settlement_observations(
        [row for row, _ in pairs],
    )
    assert {row["order_id"]: row["status"] for row in observations["results"]} == {
        "ho-1": "PARTIAL", "ho-3": "CANCELLED", "ho-4": "FILLED", "ho-5": "REJECTED",
    }
    assert observations["results"][0]["filled_quantity"] == 25
    assert observations["pending_order_ids"] == ["ho-1", "ho-2"]


def test_mismatched_order_does_not_suppress_other_confirmed_fill(tmp_path):
    good, good_broker = _submission(1, 56, 100)
    wrong, wrong_broker = _submission(2, 56, 100)
    wrong_broker.account_id = "OTHER-ACCOUNT"
    observations = _gateway(_cfg(tmp_path), [good_broker, wrong_broker]).settlement_observations([good, wrong])
    assert [row["order_id"] for row in observations["results"]] == ["ho-1"]
    assert observations["pending_order_ids"] == ["ho-2"]
    assert "account" in observations["issues"][0]["reason"]


def test_duplicate_broker_id_and_missing_order_are_not_terminal(tmp_path):
    row, broker = _submission(1, 56, 100)
    absent, _ = _submission(2, 56, 100)
    observations = _gateway(_cfg(tmp_path), [broker, broker]).settlement_observations([row, absent])
    assert observations["results"] == []
    assert observations["pending_order_ids"] == ["ho-1", "ho-2"]


def test_broker_vwap_is_not_rounded_before_cash_accounting(tmp_path):
    row, broker = _submission(1, 56, 100)
    broker.traded_price = 4.638041
    observations = _gateway(_cfg(tmp_path), [broker]).settlement_observations([row])
    assert observations["results"][0]["filled_price"] == 4.638041


def test_http_trade_receipt_preserves_partial_ingestion_result(monkeypatch):
    data = {
        "matched_count": 1, "unmatched_order_ids": ["old-order"],
        "rejected_observations": {"conflict-order": "NOT_SUBMITTED_CONFLICTS_WITH_BROKER_EVIDENCE"},
    }
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"code": 0, "data": data})

    monkeypatch.setattr(http_client.requests, "post", post)
    response = LiveServerClient("https://test.invalid", "TEST_ONLY").push_trade_results("20260803", [])
    assert response == data
    assert calls == [("https://test.invalid/trade-result", {
        "execution_domain": "live", "trade_date": "20260803", "results": [],
    })]


def test_settle_sends_known_fill_even_while_another_order_is_reported(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    batch = validate_order_batch(_orders(), "20260803", cfg)
    state = LiveStateStore(cfg.state_db)
    state.save_batch(batch)
    brokers = []
    for index, order in enumerate(batch.orders, start=1):
        remark = live_order_remark(order)
        state.prepare_submission(order["order_id"], batch.batch_sha256, remark)
        state.complete_submission(order["order_id"], str(index), "SUBMITTED")
        broker = _qmt_order(
            order_id=index, status=50 if index == 1 else 56,
            remark=remark, symbol=order["symbol"], traded_volume=0 if index == 1 else 100,
        )
        broker.price = order["limit_price"]
        brokers.append(broker)
    gateway = _gateway(cfg, brokers)
    monkeypatch.setattr(cli, "_gateway", lambda *_: gateway)
    pushed = []

    class Server:
        def __init__(self, *_args, **_kwargs):
            pass

        def push_trade_results(self, date, results):
            pushed.extend(results)
            return {"matched_count": len(results), "unmatched_order_ids": []}

    monkeypatch.setattr(cli, "LiveServerClient", Server)
    result = cli.settle(cfg, "20260803", None)
    assert [row["order_id"] for row in pushed] == [batch.orders[1]["order_id"]]
    assert result["pending_order_ids"] == [batch.orders[0]["order_id"]]
    assert result["status"] == "FACTS_RECORDED_PENDING"
    # No final close/reconciliation endpoint is called on unresolved facts.
    assert cli.settle_and_close(cfg, "20260803", None)["status"] == "WAITING_FOR_BROKER"
