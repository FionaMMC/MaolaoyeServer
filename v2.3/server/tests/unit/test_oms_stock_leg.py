"""V713 stock leg: rule expansion, derived targets, liquidation and monthly state."""
import json

import pandas as pd
import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState
from app.oms.cycles import CycleConflict, CycleService
from app.oms.instruments import InstrumentSpec, instrument_to_dict
from app.oms.ledger import OrderLedger
from app.oms.models import OmsCycle, OmsOrder, V713StateRecord
from app.oms.planner import Policy, policy_to_dict
from app.oms.v713 import build_publication, validate_v713_policy
from app.oms.v713_stock_leg import (STATE_STOCK_WEIGHT, derive_stock_leg, expand_policy, instrument_rule,
                                    record_state)
from app.services.reconcile import ReconcileService
from app.services.settlement import SettlementService
from app.settings import Settings
from plugins.v713_relay import basket_hash
from scripts import oms_ops

CAL = ["20260831", "20260901", "20260929", "20260930", "20261008", "20261009", "20261012",
       "20261013", "20261014", "20261015", "20261016", "20261019", "20261020", "20261021"]
NOW = "2026-10-07T20:00:00+08:00"
ALIAS = "shared"
INSTANCE = "live_v713_stockleg"
CLOSES = {"600001.SH": 10.0, "600002.SH": 20.0, "511260.SH": 120.0}


def _template():
    board = instrument_to_dict(InstrumentSpec("stock", .01, commission_rate=.0002, stamp_duty_sell=.0005))
    star = instrument_to_dict(InstrumentSpec("stock", .01, buy_lot=1, min_buy_quantity=200, sell_lot=1,
                                             commission_rate=.0002, stamp_duty_sell=.0005, min_sell_quantity=200))
    etf = instrument_to_dict(InstrumentSpec("etf", .001, commission_rate=.0002))
    base = policy_to_dict(Policy(policy_id="V713_STOCKLEG_TEST", strategy_id="v713", require_instrument_specs=True,
                                 reserve=.01, buy_bps=()))
    base.pop("instruments")
    return base | {"instrument_rules": {"stock_board_lot": board, "stock_star": star, "etf": etf}}


def test_instrument_rules_cover_board_lot_star_and_etf():
    assert instrument_rule("600001.SH") == "stock_board_lot"
    assert instrument_rule("000001.SZ") == "stock_board_lot"
    assert instrument_rule("300750.SZ") == "stock_board_lot"
    assert instrument_rule("688981.SH") == "stock_star"
    assert instrument_rule("511260.SH") == "etf"
    assert instrument_rule("159915.SZ") == "etf"
    with pytest.raises(ValueError, match="830799.BJ"):
        instrument_rule("830799.BJ")


def test_expand_policy_gives_every_symbol_an_explicit_spec():
    symbols = ["600001.SH", "688001.SH", "511260.SH"]
    payload = expand_policy(_template(), symbols)
    assert "instrument_rules" not in payload
    assert payload["instruments"]["688001.SH"]["min_buy_quantity"] == 200
    assert payload["instruments"]["511260.SH"]["asset_type"] == "etf"
    validate_v713_policy(payload, symbols)


def test_expand_policy_requires_exactly_the_three_rules():
    template = _template()
    del template["instrument_rules"]["etf"]
    with pytest.raises(ValueError, match="exactly"):
        expand_policy(template, ["600001.SH"])


def _policy():
    return expand_policy(_template(), ["600001.SH", "600002.SH", "511260.SH"])


def _service(tmp_path, positions=None, cash=200000.0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = make_engine(f"sqlite:///{tmp_path}/oms.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(InstanceState(instance_id=INSTANCE, execution_domain="live", account_alias=ALIAS,
                            ledger_mode="attributed", virtual_cash=cash, virtual_positions=dict(positions or {}),
                            owned_symbols=sorted(positions or {}), last_update="x"))
        s.commit()
    settlement = SettlementService(sf, commission_rate=.0002, min_commission=5.0, stamp_duty_sell=.0005)
    return sf, CycleService(sf, OrderLedger(sf, settlement), ReconcileService(sf))


def _liquidate(svc, **changes):
    return svc.publish_target(**({"instance_id": INSTANCE, "account_alias": ALIAS, "signal_date": "20260930",
                                  "weights": {}, "signal_closes": CLOSES, "calendar": CAL,
                                  "source_sha256": "a" * 64, "now": NOW, "policy": _policy(),
                                  "earliest_execution_date": "20261008", "liquidate": True} | changes))


def test_liquidation_target_sells_every_holding(tmp_path):
    sf, svc = _service(tmp_path, positions={"600001.SH": 1000, "600002.SH": 500}, cash=5000.0)
    out = _liquidate(svc)
    assert {row["symbol"]: row["target"] for row in out["shares"]} == {"600001.SH": 0, "600002.SH": 0}
    with sf() as s:
        orders = s.query(OmsOrder).order_by(OmsOrder.symbol).all()
        assert [(o.symbol, o.side) for o in orders] == [("600001.SH", "SELL"), ("600002.SH", "SELL")]


def test_liquidation_target_rejects_weights_and_an_instance_without_stock(tmp_path):
    _, svc = _service(tmp_path / "held", positions={"600001.SH": 1000})
    with pytest.raises(ValueError, match="carries no weights"):
        _liquidate(svc, weights={"600001.SH": 1.0})
    _, svc = _service(tmp_path / "cash")
    with pytest.raises(ValueError, match="nothing to liquidate"):
        _liquidate(svc)


def _basket(tmp_path, rows, sleeve, *, decision="20261008", as_of="20260930"):
    frame = pd.DataFrame([{"code": code, "weight": weight, "strategy_version": "v7.13-base", "sleeve": sleeve,
                           "decision_date": decision, "as_of_date": as_of} for code, weight in rows])
    frame["basket_sha256"] = basket_hash(frame)
    path = tmp_path / f"{sleeve}.parquet"
    frame.to_parquet(path, index=False)
    return path


def _base(path, held=()):
    return build_publication(path, closes=CLOSES, calendar=CAL, now=NOW, held_symbols=list(held),
                             policy_payload=_policy())


def test_t1_basket_rescales_stock_names_to_the_whole_leg(tmp_path):
    path = _basket(tmp_path, [("600001.SH", .25), ("600002.SH", .25), ("511260.SH", .5)], "T1_5050")
    leg = derive_stock_leg(_base(path))
    assert leg["weights"] == {"600001.SH": .5, "600002.SH": .5}
    assert leg["liquidate"] is False
    assert leg["provenance"]["sleeve"] == "T1_5050"
    assert leg["provenance"]["transform"] == "v713-stock-leg-v1"
    assert leg["provenance"]["source_allocation_sha256"] != leg["source_sha256"]


def test_top50_basket_is_unchanged_and_aux_basket_liquidates(tmp_path):
    top = derive_stock_leg(_base(_basket(tmp_path, [("600001.SH", .6), ("600002.SH", .4)], "TOP50")))
    assert top["weights"] == {"600001.SH": .6, "600002.SH": .4}
    aux = derive_stock_leg(_base(_basket(tmp_path, [("511260.SH", 1.)], "AUX_HYDRA"), held=["600001.SH"]))
    assert aux["weights"] == {} and aux["liquidate"] is True


def test_stock_weight_must_match_the_sleeve(tmp_path):
    assert STATE_STOCK_WEIGHT == {"TOP50": 1.0, "T1_5050": .5, "AUX_HYDRA": 0.0}
    path = _basket(tmp_path, [("600001.SH", .7), ("511260.SH", .3)], "T1_5050")
    with pytest.raises(ValueError, match="expected 0.5"):
        derive_stock_leg(_base(path))


def test_state_record_is_idempotent_and_frozen(tmp_path):
    sf, _ = _service(tmp_path)
    leg = derive_stock_leg(_base(_basket(tmp_path, [("600001.SH", 1.)], "TOP50")))
    assert record_state(sf, instance_id=INSTANCE, payload=leg, target_version_id=None, now=NOW)["sleeve"] == "TOP50"
    for _ in range(2):
        out = record_state(sf, instance_id=INSTANCE, payload=leg, target_version_id="tv_1", now=NOW)
        assert out["target_version_id"] == "tv_1"
    with pytest.raises(CycleConflict, match="another target version"):
        record_state(sf, instance_id=INSTANCE, payload=leg, target_version_id="tv_2", now=NOW)
    other = derive_stock_leg(_base(_basket(tmp_path, [("600001.SH", .5), ("511260.SH", .5)], "T1_5050")))
    with pytest.raises(CycleConflict, match="different allocation"):
        record_state(sf, instance_id=INSTANCE, payload=other, target_version_id=None, now=NOW)
    with sf() as s:
        row = s.get(V713StateRecord, (INSTANCE, "20260930"))
        assert (row.sleeve, row.decision_date, row.target_version_id) == ("TOP50", "20261008", "tv_1")


def _cli_args(tmp_path, basket, *, calendar=CAL, template=None, now=NOW):
    args = ["publish-v713-stock-leg", "--instance", INSTANCE, "--account", ALIAS, "--basket", str(basket),
            "--now", now]
    for name, value in {"policy-template": template or _template(), "closes": CLOSES, "calendar": calendar}.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value))
        args += [f"--{name}", str(path)]
    return args


def _settings(tmp_path):
    return Settings(_env_file=None, db_url=f"sqlite:///{tmp_path}/oms.db")


def test_cli_t1_month_publishes_rescaled_target_and_records_state(tmp_path, capsys):
    sf, _ = _service(tmp_path)
    basket = _basket(tmp_path, [("600001.SH", .25), ("600002.SH", .25), ("511260.SH", .5)], "T1_5050")
    args = _cli_args(tmp_path, basket)
    assert oms_ops.main(args, session_factory=sf, settings=_settings(tmp_path)) == 0
    assert "DRY RUN" in capsys.readouterr().out
    with sf() as s:
        assert s.query(OmsCycle).count() == 0 and s.query(V713StateRecord).count() == 0
    for _ in range(2):
        assert oms_ops.main(args + ["--apply"], session_factory=sf, settings=_settings(tmp_path)) == 0
    with sf() as s:
        cycle = s.query(OmsCycle).one()
        assert set(cycle.frozen_target) == {"600001.SH", "600002.SH"}
        state = s.query(V713StateRecord).one()
        assert state.sleeve == "T1_5050" and state.target_version_id == cycle.target_version_id


def test_cli_aux_month_liquidates_held_stock(tmp_path):
    sf, _ = _service(tmp_path, positions={"600001.SH": 1000}, cash=1000.0)
    basket = _basket(tmp_path, [("511260.SH", 1.)], "AUX_HYDRA")
    assert oms_ops.main(_cli_args(tmp_path, basket) + ["--apply"], session_factory=sf,
                        settings=_settings(tmp_path)) == 0
    with sf() as s:
        assert s.query(OmsCycle).one().frozen_target == {"600001.SH": 0}
        assert s.query(V713StateRecord).one().sleeve == "AUX_HYDRA"


def test_cli_aux_month_with_cash_only_records_state(tmp_path, capsys):
    sf, _ = _service(tmp_path)
    basket = _basket(tmp_path, [("511260.SH", 1.)], "AUX_HYDRA")
    assert oms_ops.main(_cli_args(tmp_path, basket) + ["--apply"], session_factory=sf,
                        settings=_settings(tmp_path)) == 0
    assert "STATE_ONLY" in capsys.readouterr().out
    with sf() as s:
        assert s.query(OmsCycle).count() == 0
        assert s.query(V713StateRecord).one().target_version_id is None


from pathlib import Path

from app.oms.instruments import estimate_fees, instrument_from_dict

TEMPLATE_FILE = Path(__file__).resolve().parents[2] / "plugins" / "v713" / "stockleg_policy_template_v1.json"


def test_frozen_template_carries_the_confirmed_execution_and_fee_rules():
    template = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
    payload = expand_policy(template, ["600001.SH", "300750.SZ", "688981.SH", "511260.SH"])
    policy = validate_v713_policy(payload, list(payload["instruments"]))
    assert (policy.default_buy_bps, policy.sell_schedule, policy.buy_bps, policy.reserve) == (50.0, (50.0, 200.0), (), .01)
    board = payload["instruments"]["600001.SH"]
    assert (board["commission_rate"], board["min_commission"], board["stamp_duty_sell"]) == (.0000854, 5.0, .0005)
    assert payload["instruments"]["688981.SH"]["min_buy_quantity"] == 200
    assert payload["instruments"]["688981.SH"]["min_sell_quantity"] == 200
    spec = instrument_from_dict(board)
    # Above the minimum: 8.54 commission + 1.00 transfer fee + 50.00 stamp duty.
    assert estimate_fees(spec, 100_000.0, "SELL") == pytest.approx(59.54)
    # The 5-yuan minimum applies to commission alone, not to all fees together.
    assert estimate_fees(spec, 10_000.0, "BUY") == pytest.approx(5.10)


from datetime import datetime, timedelta, timezone

from app.oms.models import OmsSession, OmsTargetVersion
from app.oms.schemas import Quote, SnapshotIn

CN = timezone(timedelta(hours=8))
CAL_LONG = CAL + ["20261022", "20261023", "20261026", "20261027", "20261028", "20261029", "20261030",
                  "20261102", "20261103", "20261104", "20261105", "20261106", "20261109", "20261110",
                  "20261111", "20261112", "20261113", "20261116", "20261117"]
TEMPLATE_V2 = Path(__file__).resolve().parents[2] / "plugins" / "v713" / "stockleg_policy_template_v2.json"


def _policy_v2(**changes):
    return _policy() | {"policy_id": "V713_STOCKLEG_TEST_V2", "anchor": "previous_close", "window": 10} | changes


def _eod(svc, day, closes, positions, cash):
    stamp = f"{day[:4]}-{day[4:6]}-{day[6:]}T15:00:05+08:00"
    snap = SnapshotIn(account_alias=ALIAS, kind="EOD", trade_date=day,
                      taken_at=datetime(int(day[:4]), int(day[4:6]), int(day[6:]), 15, 30, tzinfo=CN),
                      available_cash=cash, total_asset=cash, positions=positions, sellable=positions, orders=[],
                      trades=None, quotes={s: Quote(last_price=p, source_time=stamp, adjustment="none",
                                                    price_source="raw_daily_close") for s, p in closes.items()})
    return svc.ingest_snapshot(snap, now=snap.taken_at.isoformat())


def _second_sell_limit(tmp_path, policy):
    sf, svc = _service(tmp_path, positions={"600001.SH": 1000}, cash=5000.0)
    out = _liquidate(svc, policy=policy, calendar=CAL_LONG)
    svc.approve(out["cycle_id"], "tester", NOW)
    for day, close in (("20261008", 9.0), ("20261009", 9.2)):
        _eod(svc, day, {"600001.SH": close}, {"600001.SH": 1000}, 5000.0)
    with sf() as s:
        order = s.query(OmsOrder).filter_by(trade_date="20261012", side="SELL").one()
        return order.limit_price, order.reference_price


def test_previous_close_anchor_reprices_every_session_from_the_latest_close(tmp_path):
    assert _second_sell_limit(tmp_path / "v2", _policy_v2()) == (9.02, 9.2)
    assert _second_sell_limit(tmp_path / "fixed", _policy() | {"window": 10}) == (9.8, 10.0)


def test_late_previous_close_publication_needs_the_previous_day_closes(tmp_path):
    _, svc = _service(tmp_path, positions={"600001.SH": 1000}, cash=5000.0)
    late = {"policy": _policy_v2(), "calendar": CAL_LONG, "now": "2026-10-09T20:00:00+08:00"}
    with pytest.raises(ValueError, match="20261009"):
        _liquidate(svc, **late)
    out = _liquidate(svc, **late, reference_closes={"600001.SH": 9.5})
    assert [x["limit_price"] for x in out["first_session"]["orders"]] == [9.46]


def _publish_month(svc, signal, now, **changes):
    return svc.publish_target(**({"instance_id": INSTANCE, "account_alias": ALIAS, "signal_date": signal,
                                  "weights": {"600002.SH": 1.0}, "signal_closes": CLOSES, "calendar": CAL_LONG,
                                  "source_sha256": signal * 8, "now": now, "policy": _policy()} | changes))


def test_a_new_month_target_supersedes_the_open_cycle(tmp_path):
    sf, svc = _service(tmp_path, positions={"600001.SH": 5000}, cash=150000.0)
    old = _publish_month(svc, "20260930", NOW)
    with pytest.raises(CycleConflict, match="open cycle"):
        _publish_month(svc, "20261030", "2026-10-30T20:00:00+08:00")
    with pytest.raises(CycleConflict, match="later signal"):
        _publish_month(svc, "20260929", "2026-10-30T20:00:00+08:00", supersede=True)
    new = _publish_month(svc, "20261030", "2026-10-30T20:00:00+08:00", supersede=True)
    with sf() as s:
        closed = s.get(OmsCycle, old["cycle_id"])
        assert (closed.status, closed.close_reason) == ("CLOSED", "SUPERSEDED")
        assert {o.state for o in s.query(OmsOrder).filter_by(cycle_id=old["cycle_id"])} == {"NOT_SUBMITTED"}
        assert s.query(OmsSession).filter_by(cycle_id=old["cycle_id"], status="PLANNED").count() == 0
        assert s.get(OmsCycle, new["cycle_id"]).status == "PENDING_APPROVAL"


def test_supersede_refuses_while_an_order_is_at_the_broker(tmp_path):
    sf, svc = _service(tmp_path, positions={"600001.SH": 5000}, cash=150000.0)
    old = _publish_month(svc, "20260930", NOW)
    with sf() as s:
        s.query(OmsOrder).filter_by(cycle_id=old["cycle_id"]).update({"state": "ACKED"})
        s.commit()
    with pytest.raises(CycleConflict, match="in flight"):
        _publish_month(svc, "20261030", "2026-10-30T20:00:00+08:00", supersede=True)


def test_frozen_v2_template_is_option_b_with_a_ten_session_window():
    template = json.loads(TEMPLATE_V2.read_text(encoding="utf-8"))
    payload = expand_policy(template, ["600001.SH", "688981.SH", "511260.SH"])
    policy = validate_v713_policy(payload, list(payload["instruments"]))
    assert (policy.policy_id, policy.anchor, policy.window) == ("V713_STOCKLEG_V2", "previous_close", 10)
    assert (policy.default_buy_bps, policy.sell_schedule, policy.buy_bps, policy.reserve) == (50.0, (50.0, 200.0), (), .01)
    assert payload["instruments"]["688981.SH"]["min_sell_quantity"] == 200
    assert payload["instruments"]["600001.SH"]["commission_rate"] == .0000854


def test_cli_next_month_supersedes_the_open_cycle(tmp_path, capsys):
    sf, _ = _service(tmp_path, positions={"600001.SH": 5000}, cash=150000.0)
    (tmp_path / "sep").mkdir()
    first = _basket(tmp_path / "sep", [("600002.SH", 1.0)], "TOP50")
    args = _cli_args(tmp_path, first, calendar=CAL_LONG, template=_template() | {"window": 10})
    assert oms_ops.main(args + ["--apply"], session_factory=sf, settings=_settings(tmp_path)) == 0
    (tmp_path / "oct").mkdir()
    second = _basket(tmp_path / "oct", [("600001.SH", 1.0)], "TOP50", decision="20261102", as_of="20261030")
    later = _cli_args(tmp_path, second, calendar=CAL_LONG, template=_template() | {"window": 10},
                      now="2026-11-01T20:00:00+08:00")
    assert oms_ops.main(later, session_factory=sf, settings=_settings(tmp_path)) == 0
    assert "WILL SUPERSEDE C00001" in capsys.readouterr().out
    assert oms_ops.main(later + ["--apply"], session_factory=sf, settings=_settings(tmp_path)) == 0
    with sf() as s:
        assert [(c.cycle_id, c.status) for c in s.query(OmsCycle).order_by(OmsCycle.cycle_no)] == [
            ("C00001", "CLOSED"), ("C00002", "PENDING_APPROVAL")]
        assert s.query(V713StateRecord).count() == 2


def test_cli_aux_month_in_cash_still_ends_the_open_cycle(tmp_path):
    sf, _ = _service(tmp_path)
    (tmp_path / "sep").mkdir()
    first = _basket(tmp_path / "sep", [("600002.SH", 1.0)], "TOP50")
    assert oms_ops.main(_cli_args(tmp_path, first, calendar=CAL_LONG) + ["--apply"], session_factory=sf,
                        settings=_settings(tmp_path)) == 0
    (tmp_path / "oct").mkdir()
    aux = _basket(tmp_path / "oct", [("511260.SH", 1.0)], "AUX_HYDRA", decision="20261102", as_of="20261030")
    assert oms_ops.main(_cli_args(tmp_path, aux, calendar=CAL_LONG, now="2026-11-01T20:00:00+08:00") + ["--apply"],
                        session_factory=sf, settings=_settings(tmp_path)) == 0
    with sf() as s:
        cycle = s.query(OmsCycle).one()
        assert (cycle.status, cycle.close_reason) == ("CLOSED", "SUPERSEDED")
        assert [r.sleeve for r in s.query(V713StateRecord).order_by(V713StateRecord.signal_date)] == ["TOP50", "AUX_HYDRA"]
