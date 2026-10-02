from decimal import Decimal

import pytest

from app.models import CashFlowJournal, DividendEntitlement, InstanceState, PerfSnapshot
from app.services.dividend_entitlement import (
    DividendEntitlementService,
    DividendRequest,
    outstanding_dividends,
)
from app.services.perf import PerfService
from tests.unit.test_perf import _setup, _bar
import pandas as pd


def setup_right(tmp_path, *, instance="live", domain="live", qty="1200"):
    sf, store = _setup(tmp_path)
    with sf() as s:
        s.add(
            InstanceState(
                instance_id=instance,
                execution_domain=domain,
                account_alias="test",
                virtual_cash=100,
                virtual_positions={"511260.SH": float(qty)},
                last_update="20260917",
            )
        )
        s.commit()
    req = DividendRequest(
        execution_domain=domain,
        account_alias="test",
        instance_id=instance,
        symbol="511260.SH",
        record_date="20260917",
        ex_date="20260918",
        pay_date="20260923",
        entitled_quantity=qty,
        cash_per_share="1.2747",
        evidence_sha256="a" * 64,
        settlement_source="broker-dividends",
        settlement_event_id="511260-20260918/" + instance,
    )
    return sf, store, req


def credit(sf, req, *, event_date="20260923", amount=None, instance=None):
    amount = float(req.record()["amount"]) if amount is None else amount
    with sf() as s:
        s.add(
            CashFlowJournal(
                execution_domain=req.execution_domain,
                account_alias="test",
                instance_id=instance or req.instance_id,
                event_date=event_date,
                event_type="DIVIDEND",
                amount=amount,
                source=req.settlement_source,
                source_event_id=req.settlement_event_id,
                evidence_sha256="a" * 64,
                status="APPLIED",
                created_at="now",
                applied_at="now",
            )
        )
        s.get(InstanceState, req.instance_id).virtual_cash += amount
        s.commit()


def test_dividend_ex_date_and_payment_do_not_create_fake_loss_or_double_income(tmp_path):
    sf, store, req = setup_right(tmp_path)
    DividendEntitlementService(sf).register(req, apply=True)
    store.append(
        "etfs",
        "511260.SH",
        pd.DataFrame([_bar(20260917, 135.970), _bar(20260918, 134.739), _bar(20260923, 134.739)]),
    )
    perf = PerfService(sf, store)
    perf.snapshot_all(20260917, "live")
    perf.snapshot_all(20260918, "live")
    with sf() as s:
        before = s.get(PerfSnapshot, ("live", "20260917"))
        after = s.get(PerfSnapshot, ("live", "20260918"))
        assert after.nav - before.nav == pytest.approx(52.44)
        assert s.get(InstanceState, "live").virtual_cash == 100  # not spendable
    credit(sf, req)
    perf.snapshot_all(20260923, "live")
    with sf() as s:
        assert s.get(PerfSnapshot, ("live", "20260923")).nav == after.nav
        assert s.get(PerfSnapshot, ("live", "20260923")).daily_return == 0
        assert outstanding_dividends(s, "live", "live", "20260918") == Decimal("1529.64")
        assert outstanding_dividends(s, "live", "live", "20260923") == 0


def test_registration_is_idempotent_and_preview_never_adds_cash_or_rights(tmp_path):
    sf, store, req = setup_right(tmp_path)
    svc = DividendEntitlementService(sf)
    assert svc.register(req)["applied"] is False
    with sf() as s:
        assert s.query(DividendEntitlement).count() == 0
    assert not svc.register(req, apply=True)["already_registered"]
    assert svc.register(req, apply=True)["already_registered"]
    with pytest.raises(ValueError, match="different"):
        svc.register(req.model_copy(update={"entitled_quantity": Decimal(2400)}), apply=True)
    with sf() as s:
        assert s.query(DividendEntitlement).count() == 1
        assert s.get(InstanceState, "live").virtual_cash == 100


def test_right_survives_sale_and_is_separate_for_shared_symbol(tmp_path):
    sf, store, req = setup_right(tmp_path)
    svc = DividendEntitlementService(sf)
    svc.register(req, apply=True)
    with sf() as s:
        s.get(InstanceState, "live").virtual_positions = {}
        s.add(
            InstanceState(
                instance_id="second",
                execution_domain="live",
                account_alias="test",
                virtual_cash=0,
                virtual_positions={"511260.SH": 100},
                last_update="now",
            )
        )
        s.commit()
    second = req.model_copy(
        update={
            "instance_id": "second",
            "entitled_quantity": Decimal(100),
            "settlement_event_id": "second-payment",
        }
    )
    svc.register(second, apply=True)
    with sf() as s:
        assert outstanding_dividends(s, "live", "live", "20260918") == Decimal("1529.64")
        assert outstanding_dividends(s, "second", "live", "20260918") == Decimal("127.47")
        assert outstanding_dividends(s, "live", "paper", "20260918") == 0


def test_received_cash_before_registration_is_not_counted_twice(tmp_path):
    sf, store, req = setup_right(tmp_path)
    credit(sf, req)
    DividendEntitlementService(sf).register(req, apply=True)
    with sf() as s:
        assert outstanding_dividends(s, "live", "live", "20260923") == 0


@pytest.mark.parametrize(
    "wrong", [{"amount": 1.0}, {"instance": "other"}, {"event_date": "20260916"}]
)
def test_mismatched_payment_never_silently_removes_right(tmp_path, wrong):
    sf, store, req = setup_right(tmp_path)
    credit(sf, req, **wrong)
    with pytest.raises(ValueError, match="differs"):
        DividendEntitlementService(sf).register(req, apply=True)
    with sf() as s:
        assert s.query(DividendEntitlement).count() == 0
        assert s.query(CashFlowJournal).count() == 1  # observed cash survives


def test_payment_date_does_not_automatically_credit_cash(tmp_path):
    sf, store, req = setup_right(tmp_path, domain="paper", instance="paper", qty="49500")
    DividendEntitlementService(sf).register(req, apply=True)
    with sf() as s:
        assert outstanding_dividends(s, "paper", "paper", "20260924") == Decimal("63097.65")
        assert s.get(InstanceState, "paper").virtual_cash == 100


def test_invalid_dates_and_nonfinite_amounts_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DividendRequest(
            execution_domain="live",
            account_alias="a",
            instance_id="x",
            symbol="511260.SH",
            record_date="20260999",
            ex_date="20260918",
            pay_date="20260923",
            entitled_quantity="NaN",
            cash_per_share="1",
            evidence_sha256="a" * 64,
            settlement_source="s",
            settlement_event_id="e",
        )


def test_risk_report_excludes_receivable_from_cash_and_retains_dividend_return(tmp_path):
    from app.models import DailyRiskSnapshot
    from app.services.daily_risk import DailyRiskSnapshotService

    sf, store, req = setup_right(tmp_path)
    DividendEntitlementService(sf).register(req, apply=True)
    store.append(
        "etfs",
        "511260.SH",
        pd.DataFrame(
            [
                _bar(20260917, 135.970),
                _bar(20260918, 134.739),
                _bar(20260923, 134.739),
            ]
        ),
    )
    perf = PerfService(sf, store)
    perf.snapshot_all(20260917, "live")
    perf.snapshot_all(20260918, "live")
    credit(sf, req)
    perf.snapshot_all(20260923, "live")
    DailyRiskSnapshotService(sf, store).rebuild(instance_id="live")
    with sf() as s:
        ex = s.get(DailyRiskSnapshot, ("live", "20260918"))
        paid = s.get(DailyRiskSnapshot, ("live", "20260923"))
        assert ex.cash == 100
        assert ex.cash_source == "snapshot"
        assert paid.cash == pytest.approx(1629.64)
        assert paid.external_cash_flow == 0  # investment income, not a contribution
        assert paid.portfolio_return == 0
        assert ex.portfolio_return > 0


def test_registering_right_does_not_reinterpret_legacy_nav_as_restatement(tmp_path):
    from app.models import DailyRiskSnapshot
    from app.services.daily_risk import DailyRiskSnapshotService

    sf, store, req = setup_right(tmp_path)
    store.append("etfs", "511260.SH", pd.DataFrame([_bar(20260918, 134.739)]))
    with sf() as s:
        s.add(
            PerfSnapshot(
                instance_id="live",
                date="20260918",
                execution_domain="live",
                nav=100 + 1200 * 134.739,
                daily_return=None,
                positions_snapshot={"511260.SH": 1200},
            )
        )
        s.commit()
    DividendEntitlementService(sf).register(req, apply=True)
    DailyRiskSnapshotService(sf, store).rebuild(instance_id="live")
    with sf() as s:
        row = s.get(DailyRiskSnapshot, ("live", "20260918"))
        assert row.cash == pytest.approx(100)
        assert row.cash_source == "nav_residual"
