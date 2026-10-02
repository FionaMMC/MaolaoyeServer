"""Live delivery and expiry/late-fill invalidation share a short SQLite lock."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

import pytest
from sqlalchemy import event

from app.db import init_db, make_engine, make_session_factory
from app.models import HydraExecutionAttempt, Order
from app.services.ledger_transaction import begin_ledger_transaction
from app.services.orders_queue import OrdersQueueService


TRADE_DATE = "20260910"
ACCOUNT = "isolated-test-account"


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/delivery-lock.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        session.add(HydraExecutionAttempt(
            attempt_id="successor", rebalance_id="rebalance", execution_domain="live",
            account_alias=ACCOUNT, attempt_number=2, trade_date=TRADE_DATE,
            residual_before={"ETF": 100}, risk_snapshot={}, batch_id="batch",
            batch_sha256="a" * 64, status="PENDING",
            created_at="2026-09-09T08:00:00+00:00",
        ))
        session.add(Order(
            order_id="successor-order", execution_domain="live", qmt_account_alias=ACCOUNT,
            attempt_id="successor", attempt_number=2, rebalance_id="rebalance",
            target_id="target", batch_id="batch", batch_sha256="a" * 64,
            account_group=ACCOUNT, symbol="ETF", direction="BUY", quantity=100,
            limit_price=5, valid_date=TRADE_DATE, status="PENDING",
            created_at="2026-09-09T08:00:00+00:00",
        ))
        session.commit()
    yield engine, factory
    engine.dispose()


@pytest.mark.parametrize("already_fetched", [False, True])
def test_invalidation_wins_and_live_query_cannot_deliver_stale_selection(db, already_fetched):
    engine, factory = db
    if already_fetched:
        with factory() as session:
            session.get(Order, "successor-order").fetched_at = "earlier-delivery"
            session.commit()
    reader_entered_lock = Event()

    def before_execute(_conn, _cursor, statement, _parameters, _context, _many):
        if current_thread().name.startswith("delivery-reader") and statement.strip() == "BEGIN IMMEDIATE":
            reader_entered_lock.set()

    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="delivery-reader") as pool:
            with factory() as writer:
                begin_ledger_transaction(writer, "live", ACCOUNT)
                writer.get(HydraExecutionAttempt, "successor").status = "CLOSED_PENDING_RECONCILIATION"
                writer.flush()
                result = pool.submit(
                    OrdersQueueService(factory).list_pending, TRADE_DATE, "live", (ACCOUNT,),
                )
                try:
                    assert reader_entered_lock.wait(2), "live selection must acquire the common transaction lock"
                    assert not result.done(), "reader must wait for the known invalidation to commit"
                    writer.commit()
                finally:
                    writer.rollback()
            assert result.result(timeout=5) == []
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)
    with factory() as session:
        order = session.get(Order, "successor-order")
        assert order.status == "PENDING", "invalidation does not rewrite frozen orders"
        assert order.fetched_at == ("earlier-delivery" if already_fetched else None)


def test_delivery_wins_and_invalidation_observes_the_persisted_delivery_stamp(db):
    engine, factory = db
    reader_selected = Event()
    release_reader = Event()
    writer_entered_lock = Event()

    def before_execute(_conn, _cursor, statement, _parameters, _context, _many):
        if current_thread().name.startswith("invalidation-writer") and statement.strip() == "BEGIN IMMEDIATE":
            writer_entered_lock.set()

    def after_execute(_conn, _cursor, statement, _parameters, _context, _many):
        if (
            current_thread().name.startswith("delivery-reader")
            and statement.lstrip().startswith("SELECT orders.")
            and "orders.status =" in statement
        ):
            reader_selected.set()
            assert release_reader.wait(3), "test must release the paused delivery transaction"

    def invalidate():
        with factory() as session:
            begin_ledger_transaction(session, "live", ACCOUNT)
            fetched_at = session.get(Order, "successor-order").fetched_at
            session.get(HydraExecutionAttempt, "successor").status = "CLOSED_PENDING_RECONCILIATION"
            session.commit()
            return fetched_at

    event.listen(engine, "before_cursor_execute", before_execute)
    event.listen(engine, "after_cursor_execute", after_execute)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="delivery-reader") as readers:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="invalidation-writer") as writers:
                result = readers.submit(
                    OrdersQueueService(factory).list_pending, TRADE_DATE, "live", (ACCOUNT,),
                )
                try:
                    assert reader_selected.wait(2), "reader must reach selection while holding its transaction"
                    invalidated = writers.submit(invalidate)
                    assert writer_entered_lock.wait(2)
                    assert not invalidated.done(), "invalidation must not overtake the delivery stamp"
                finally:
                    release_reader.set()
                assert [item.order_id for item in result.result(timeout=5)] == ["successor-order"]
                assert invalidated.result(timeout=5) is not None
    finally:
        release_reader.set()
        event.remove(engine, "before_cursor_execute", before_execute)
        event.remove(engine, "after_cursor_execute", after_execute)
    assert OrdersQueueService(factory).list_pending(TRADE_DATE, "live", (ACCOUNT,)) == []


def test_live_selection_error_releases_the_lock_for_next_monetary_writer(db):
    engine, factory = db

    def fail_selection(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("SELECT orders."):
            raise RuntimeError("simulated delivery query failure")

    event.listen(engine, "before_cursor_execute", fail_selection)
    try:
        with pytest.raises(RuntimeError, match="simulated delivery query failure"):
            OrdersQueueService(factory).list_pending(TRADE_DATE, "live", (ACCOUNT,))
    finally:
        event.remove(engine, "before_cursor_execute", fail_selection)
    with factory() as session:
        begin_ledger_transaction(session, "live", ACCOUNT)
        assert session.get(Order, "successor-order").fetched_at is None
        session.get(HydraExecutionAttempt, "successor").status = "CLOSED_PENDING_RECONCILIATION"
        session.commit()
    assert OrdersQueueService(factory).list_pending(TRADE_DATE, "live", (ACCOUNT,)) == []
