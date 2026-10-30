"""First-entry operator preview must describe exactly the immutable published plan."""
import json

import pytest

from app.models import InstanceState
from app.oms.cycles import CycleConflict
from app.oms.models import OmsCycle, OmsOrder, OmsOverride, OmsSession, OmsTargetVersion, V713StateRecord
from scripts import oms_ops
from tests.unit.test_oms_stock_leg import (
    CAL_LONG, INSTANCE, TEMPLATE_V2, _basket, _cli_args, _service, _settings,
)


def _inputs(tmp_path, *, now="2026-10-30T20:00:00+08:00", evidence_day="20261030", positions=None):
    sf, service = _service(tmp_path, positions=positions)
    basket = _basket(tmp_path, [("600002.SH", 1.)], "TOP50", decision="20261102", as_of="20261030")
    args = _cli_args(tmp_path, basket, calendar=CAL_LONG,
                     template=json.loads(TEMPLATE_V2.read_text()), now=now)
    date = f"{evidence_day[:4]}-{evidence_day[4:6]}-{evidence_day[6:]}"
    evidence = {"trade_date": evidence_day, "taken_at": f"{date}T15:30:00+08:00", "quotes": {
        "600002.SH": {"last_price": 20., "source_time": f"{date}T15:00:05+08:00",
                        "adjustment": "none", "price_source": "raw_daily_close"}}}
    path = tmp_path / "initial-close-evidence.json"
    path.write_text(json.dumps(evidence))
    return sf, service, args + ["--initial-cash-buy", "--reference-close-evidence", str(path)], path


def _preview_output(capsys):
    output = capsys.readouterr().out
    payload, _ = json.JSONDecoder().raw_decode(output[output.index("{\n"):])
    return payload


def _assert_unpublished(sf):
    with sf() as session:
        for model in (OmsCycle, OmsOrder, OmsSession, OmsTargetVersion, OmsOverride, V713StateRecord):
            assert session.query(model).count() == 0
        assert not session.get(InstanceState, INSTANCE).strategy_state


def test_friday_preview_and_publication_both_start_monday_buy(tmp_path, capsys):
    sf, _, args, _ = _inputs(tmp_path)
    assert oms_ops.main(args, session_factory=sf, settings=_settings(tmp_path)) == 0
    preview = _preview_output(capsys)
    assert preview["initial_cash_buy"] and preview["reference_date"] == "20261030"
    assert len(preview["schedule"]) == 10
    assert preview["schedule"][0]["trade_date"] == "20261102"
    assert all(x["phase"] == "BUY" for x in preview["schedule"])
    assert preview["first_session_sells"] == []
    assert len(preview["first_session_buys"]) == 1
    _assert_unpublished(sf)

    assert oms_ops.main(args + ["--apply"], session_factory=sf, settings=_settings(tmp_path)) == 0
    applied_preview = _preview_output(capsys)
    assert applied_preview == preview
    with sf() as session:
        cycle = session.query(OmsCycle).one()
        order = session.query(OmsOrder).one()
        expected = preview["first_session_buys"][0]
        assert cycle.status == "PENDING_APPROVAL" and cycle.schedule == preview["schedule"]
        assert (order.side, order.symbol, order.quantity, order.limit_price, order.reference_price) == (
            "BUY", expected["symbol"], expected["quantity"], expected["limit_price"], expected["reference_price"])
        receipt = session.query(OmsOverride).filter_by(action="INITIAL_CASH_ENTRY").one()
        assert receipt.payload["reference_date"] == "20261030"
        assert session.query(V713StateRecord).one().target_version_id == cycle.target_version_id


def test_repeat_preview_after_cutoff_returns_original_schedule_and_ignores_extra_evidence_fields(tmp_path, capsys):
    sf, _, args, path = _inputs(tmp_path)
    oms_ops.main(args + ["--apply"], session_factory=sf, settings=_settings(tmp_path))
    original = _preview_output(capsys)
    evidence = json.loads(path.read_text())
    evidence["account_alias"] = "extra-source-metadata"
    path.write_text(json.dumps(evidence))
    args[args.index("--now") + 1] = "2026-11-02T18:00:00+08:00"
    for suffix in ([], ["--apply"]):
        assert oms_ops.main(args + suffix, session_factory=sf, settings=_settings(tmp_path)) == 0
        repeated = _preview_output(capsys)
        assert repeated["schedule"] == original["schedule"]
        assert repeated["first_session_buys"][0]["limit_price"] == original["first_session_buys"][0]["limit_price"]
    with sf() as session:
        assert session.query(OmsCycle).count() == session.query(OmsOrder).count() == 1
        assert session.query(OmsOverride).filter_by(action="INITIAL_CASH_ENTRY").count() == 1


def test_changed_evidence_on_repeat_is_rejected_without_writes(tmp_path, capsys):
    sf, _, args, path = _inputs(tmp_path)
    oms_ops.main(args + ["--apply"], session_factory=sf, settings=_settings(tmp_path))
    _preview_output(capsys)
    evidence = json.loads(path.read_text())
    evidence["quotes"]["600002.SH"]["last_price"] = 21.
    path.write_text(json.dumps(evidence))
    with pytest.raises(CycleConflict, match="evidence"):
        oms_ops.main(args + ["--apply"], session_factory=sf, settings=_settings(tmp_path))
    with sf() as session:
        assert session.query(OmsCycle).count() == 1
        assert session.query(OmsOrder).one().reference_price == 20.


@pytest.mark.parametrize("field,value", [
    ("adjustment", "qfq"), ("source_time", "2026-10-30T14:59:59+08:00"),
    ("price_source", "estimated"), ("last_price", float("nan")),
])
def test_invalid_close_evidence_is_rejected_before_any_state_write(tmp_path, field, value):
    sf, _, args, path = _inputs(tmp_path)
    evidence = json.loads(path.read_text())
    evidence["quotes"]["600002.SH"][field] = value
    path.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="evidence"):
        oms_ops.main(args + ["--apply"], session_factory=sf, settings=_settings(tmp_path))
    _assert_unpublished(sf)


def test_monday_evening_uses_monday_close_and_starts_tuesday_without_empty_sell(tmp_path, capsys):
    sf, _, args, _ = _inputs(tmp_path, now="2026-11-02T18:00:00+08:00", evidence_day="20261102")
    oms_ops.main(args, session_factory=sf, settings=_settings(tmp_path))
    preview = _preview_output(capsys)
    assert preview["reference_date"] == "20261102"
    assert preview["schedule"][0]["trade_date"] == "20261103"
    assert preview["schedule"][0]["phase"] == "BUY"
    _assert_unpublished(sf)


def test_wrong_previous_session_evidence_is_rejected_after_morning_cutoff(tmp_path):
    sf, _, args, _ = _inputs(tmp_path, now="2026-11-02T09:00:00+08:00")
    with pytest.raises(ValueError, match="20261102"):
        oms_ops.main(args, session_factory=sf, settings=_settings(tmp_path))
    _assert_unpublished(sf)


def test_held_stock_cannot_use_initial_entry_flag(tmp_path):
    sf, _, args, _ = _inputs(tmp_path, positions={"600001.SH": 1000})
    with pytest.raises(CycleConflict, match="empty"):
        oms_ops.main(args + ["--apply"], session_factory=sf, settings=_settings(tmp_path))
    _assert_unpublished(sf)


def test_aux_cannot_use_initial_entry_flag(tmp_path):
    sf, _, args, _ = _inputs(tmp_path)
    aux = _basket(tmp_path, [("511260.SH", 1.)], "AUX_HYDRA", decision="20261102", as_of="20261030")
    args[args.index("--basket") + 1] = str(aux)
    with pytest.raises(ValueError, match="AUX"):
        oms_ops.main(args + ["--apply"], session_factory=sf, settings=_settings(tmp_path))
    _assert_unpublished(sf)


def test_unflagged_publication_retains_old_sell_then_buy_schedule(tmp_path, capsys):
    sf, _, args, _ = _inputs(tmp_path)
    args = args[:args.index("--initial-cash-buy")]
    oms_ops.main(args, session_factory=sf, settings=_settings(tmp_path))
    preview = _preview_output(capsys)
    assert [(s["trade_date"], s["phase"]) for s in preview["schedule"][:2]] == [
        ("20261102", "SELL"), ("20261103", "BUY")]
    assert not preview["initial_cash_buy"]


def test_flag_requires_dated_evidence_file(tmp_path):
    sf, _, args, _ = _inputs(tmp_path)
    args = args[:args.index("--reference-close-evidence")]
    with pytest.raises(SystemExit):
        oms_ops.main(args, session_factory=sf, settings=_settings(tmp_path))
    _assert_unpublished(sf)


@pytest.mark.parametrize("extra", [{"not-a-target": None}, {"600002.SH": "20.0"}])
def test_identical_price_file_retries_normalize_the_same_way(tmp_path, capsys, extra):
    sf, _, args, _ = _inputs(tmp_path)
    path = tmp_path / "closes.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | extra))
    for suffix in (["--apply"], [], ["--apply"]):
        assert oms_ops.main(args + suffix, session_factory=sf, settings=_settings(tmp_path)) == 0
        assert _preview_output(capsys)["schedule"][0]["trade_date"] == "20261102"
    with sf() as session:
        assert session.query(OmsCycle).count() == session.query(OmsOrder).count() == 1


def test_preview_remains_read_only_before_and_after_publication(tmp_path, capsys):
    from sqlalchemy import event

    sf, _, args, _ = _inputs(tmp_path)
    database = tmp_path / "oms.db"

    def query_only(session, transaction, connection):
        connection.exec_driver_sql("PRAGMA query_only=ON")

    for published in (False, True):
        if published:
            oms_ops.main(args + ["--apply"], session_factory=sf, settings=_settings(tmp_path))
            capsys.readouterr()
        before = database.read_bytes()
        event.listen(sf, "after_begin", query_only)
        try:
            assert oms_ops.main(args, session_factory=sf, settings=_settings(tmp_path)) == 0
            assert "DRY RUN" in capsys.readouterr().out
        finally:
            event.remove(sf, "after_begin", query_only)
            # Pooled connection PRAGMAs persist; return normal test settings afterwards.
            with sf.kw["bind"].connect() as conn:
                conn.exec_driver_sql("PRAGMA query_only=OFF")
        assert database.read_bytes() == before
