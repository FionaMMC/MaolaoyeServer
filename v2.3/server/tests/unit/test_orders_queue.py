"""OrdersQueueService 单元测试"""
from pathlib import Path

from app.db import init_db, make_engine, make_session_factory
from app.models import Order, OrderSignalMap
from app.services.aggregate import AggregatedOrder, OrderSignalMapping
from app.services.orders_queue import OrdersQueueService


def _factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    return make_session_factory(engine)


def _ord(order_id: str = "o1", **overrides) -> AggregatedOrder:
    base = dict(
        order_id=order_id, account_group="real_A", symbol="600519.SH",
        direction="BUY", quantity=300, limit_price=10.05, valid_date="20260430",
    )
    base.update(overrides)
    return AggregatedOrder(**base)


def test_write_aggregated_persists_orders_and_mappings(tmp_path: Path):
    f = _factory(tmp_path)
    svc = OrdersQueueService(session_factory=f)
    n = svc.write_aggregated(
        orders=[_ord(order_id="oid1")],
        mappings=[
            OrderSignalMapping(order_id="oid1", signal_id="s1", signal_quantity=100),
            OrderSignalMapping(order_id="oid1", signal_id="s2", signal_quantity=200),
        ],
    )
    assert n == 1
    with f() as s:
        assert s.get(Order, "oid1").status == "PENDING"
        maps = s.query(OrderSignalMap).filter_by(order_id="oid1").all()
        assert {m.signal_id for m in maps} == {"s1", "s2"}


def test_write_aggregated_empty_returns_zero(tmp_path: Path):
    svc = OrdersQueueService(session_factory=_factory(tmp_path))
    assert svc.write_aggregated([], []) == 0


def test_list_pending_returns_only_matching_date(tmp_path: Path):
    svc = OrdersQueueService(session_factory=_factory(tmp_path))
    svc.write_aggregated([
        _ord(order_id="o1", valid_date="20260430"),
        _ord(order_id="o2", valid_date="20260501"),
    ], [])
    items = svc.list_pending("20260430")
    assert [i.order_id for i in items] == ["o1"]


def test_list_pending_excludes_non_pending(tmp_path: Path):
    f = _factory(tmp_path)
    svc = OrdersQueueService(session_factory=f)
    svc.write_aggregated([
        _ord(order_id="o1"),
        _ord(order_id="o2"),
    ], [])
    with f() as s:
        s.get(Order, "o1").status = "FILLED"
        s.commit()
    items = svc.list_pending("20260430")
    assert [i.order_id for i in items] == ["o2"]


def test_list_pending_empty_when_no_orders(tmp_path: Path):
    svc = OrdersQueueService(session_factory=_factory(tmp_path))
    assert svc.list_pending("20260430") == []


def test_mark_status_updates(tmp_path: Path):
    f = _factory(tmp_path)
    svc = OrdersQueueService(session_factory=f)
    svc.write_aggregated([_ord(order_id="o1")], [])

    assert svc.mark_status("o1", "FILLED") is True
    with f() as s:
        assert s.get(Order, "o1").status == "FILLED"


def test_mark_status_unknown_returns_false(tmp_path: Path):
    svc = OrdersQueueService(session_factory=_factory(tmp_path))
    assert svc.mark_status("nope", "FILLED") is False


def test_list_pending_stamps_fetched_at_once(tmp_path: Path):
    """GET /orders 拉取即在 fetched_at 盖章；重复拉取保留首次时间戳。

    Regression（2026-07-02 成交未匹配事故）：客户端拉走订单后服务器重跑管线
    换掉 order_id → 次日成交回报全量 unmatched。fetched_at 是重算护栏的判据。
    """
    f = _factory(tmp_path)
    svc = OrdersQueueService(session_factory=f)
    svc.write_aggregated([_ord(order_id="o1")], [])

    with f() as s:
        assert s.get(Order, "o1").fetched_at is None

    svc.list_pending("20260430")
    with f() as s:
        first = s.get(Order, "o1").fetched_at
        assert first is not None

    svc.list_pending("20260430")
    with f() as s:
        assert s.get(Order, "o1").fetched_at == first


def test_list_pending_other_date_not_stamped(tmp_path: Path):
    f = _factory(tmp_path)
    svc = OrdersQueueService(session_factory=f)
    svc.write_aggregated([
        _ord(order_id="o1", valid_date="20260430"),
        _ord(order_id="o2", valid_date="20260501"),
    ], [])
    svc.list_pending("20260430")
    with f() as s:
        assert s.get(Order, "o1").fetched_at is not None
        assert s.get(Order, "o2").fetched_at is None
