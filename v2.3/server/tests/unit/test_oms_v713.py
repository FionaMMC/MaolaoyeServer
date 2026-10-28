import hashlib
import json

import pandas as pd
import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState
from app.oms.instruments import InstrumentSpec, instrument_to_dict
from app.oms.models import OmsCycle, OmsOrder
from app.oms.planner import Policy, policy_to_dict
from app.oms.v713 import build_publication, validate_v713_policy
from app.settings import Settings
from plugins.v713_relay import basket_hash
from scripts import oms_ops


CAL = ["20260831", "20260901", "20260929", "20260930", "20261008", "20261009", "20261012",
       "20261013", "20261014", "20261015", "20261016", "20261019", "20261020", "20261021"]
NOW = "2026-10-07T20:00:00+08:00"


def _basket(tmp_path, *, decision="20261008", as_of="20260930", weight=1.):
    frame = pd.DataFrame([{"code": "600002.SH", "weight": weight, "strategy_version": "v7.13-base",
                           "sleeve": "TOP50", "decision_date": decision, "as_of_date": as_of}])
    frame["basket_sha256"] = basket_hash(frame)
    path = tmp_path / "target.parquet"
    frame.to_parquet(path, index=False)
    return path


def _policy():
    spec = instrument_to_dict(InstrumentSpec("stock", .01, commission_rate=.0003, stamp_duty_sell=.0005))
    return policy_to_dict(Policy(policy_id="V713_ACCEPTANCE_V1", strategy_id="v713", require_instrument_specs=True,
                                instruments={"600001.SH": spec, "600002.SH": spec}, buy_bps=()))


def _request(path, **changes):
    return build_publication(path, **({"closes": {"600001.SH": 10., "600002.SH": 20.},
                                      "calendar": CAL, "policy_payload": _policy(), "now": NOW,
                                      "held_symbols": ["600001.SH"]} | changes))


def test_v713_maps_asof_and_earliest_execution_without_modifying_source(tmp_path):
    path = _basket(tmp_path)
    before = path.read_bytes()
    payload = _request(path)
    assert payload["signal_date"] == "20260930"
    assert payload["earliest_execution_date"] == "20261008"
    assert payload["weights"] == {"600002.SH": 1.}
    assert payload["policy"].strategy_id == "v713"
    assert payload["provenance"]["file_sha256"] == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before


def test_changed_publication_date_preserves_monthly_identity(tmp_path):
    one = _request(_basket(tmp_path))
    two = _request(_basket(tmp_path, decision="20261009"))
    assert one["source_sha256"] == two["source_sha256"]
    assert one["provenance"]["basket_sha256"] != two["provenance"]["basket_sha256"]


def test_rejects_tampered_or_invalid_basket(tmp_path):
    path = _basket(tmp_path)
    frame = pd.read_parquet(path)
    frame.loc[0, "code"] = "600003.SH"
    frame.to_parquet(path)
    with pytest.raises(ValueError, match="sha256 mismatch"):
        _request(path)
    with pytest.raises(ValueError, match="sum to 1"):
        _request(_basket(tmp_path, weight=.8))


def test_policy_is_explicit_and_covers_old_holdings(tmp_path):
    payload = _policy()
    del payload["sell_schedule"]
    with pytest.raises(ValueError, match="explicitly"):
        validate_v713_policy(payload)
    payload = _policy()
    del payload["instruments"]["600001.SH"]
    with pytest.raises(ValueError, match="600001.SH"):
        _request(_basket(tmp_path), policy_payload=payload)
    payload = _policy()
    del payload["instruments"]["600002.SH"]["commission_rate"]
    with pytest.raises(ValueError, match="all instrument"):
        validate_v713_policy(payload)



def test_star_instrument_must_carry_the_200_share_sell_minimum():
    payload = _policy()
    payload["instruments"]["688001.SH"] = instrument_to_dict(InstrumentSpec(
        "stock", .01, buy_lot=1, min_buy_quantity=200, sell_lot=1, commission_rate=.0003, stamp_duty_sell=.0005))
    with pytest.raises(ValueError, match="688001.SH"):
        validate_v713_policy(payload)
    payload["instruments"]["688001.SH"]["min_sell_quantity"] = 200
    validate_v713_policy(payload)

def test_calendar_rejects_ambiguous_dates_and_stale_month(tmp_path):
    with pytest.raises(ValueError, match="timezone"):
        _request(_basket(tmp_path), now="2026-10-07T20:00:00")
    with pytest.raises(ValueError, match="completed month-end"):
        _request(_basket(tmp_path, as_of="20260929"))
    with pytest.raises(ValueError, match="stale"):
        _request(_basket(tmp_path, decision="20260901", as_of="20260831"))


def test_cli_v713_dry_run_and_apply_then_duplicate_do_not_change_live_account(tmp_path, capsys):
    path = _basket(tmp_path)
    engine = make_engine(f"sqlite:///{tmp_path}/test.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(InstanceState(instance_id="live_v713", execution_domain="live", account_alias="shared",
                            ledger_mode="attributed", virtual_cash=10000., virtual_positions={"600001.SH": 100},
                            owned_symbols=["600001.SH"], last_update="x"))
        s.commit()
    files = {"policy": _policy(), "closes": {"600001.SH": 10., "600002.SH": 20.}, "calendar": CAL}
    args = ["publish-v713", "--instance", "live_v713", "--account", "shared", "--basket", str(path),
            "--now", NOW]
    for name, value in files.items():
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(value))
        args += [f"--{name}", str(p)]
    settings = Settings(_env_file=None, db_url=str(engine.url))
    assert oms_ops.main(args, session_factory=sf, settings=settings) == 0
    out = capsys.readouterr().out
    assert "600001.SH" in out and "DRY RUN" in out and "file_sha256" in out
    with sf() as s:
        assert s.query(OmsCycle).count() == 0
        assert s.get(InstanceState, "live_v713").virtual_cash == 10000.
    for _ in range(2):
        assert oms_ops.main(args + ["--apply"], session_factory=sf, settings=settings) == 0
    with sf() as s:
        assert s.query(OmsCycle).count() == 1
        cycle = s.query(OmsCycle).one()
        assert cycle.frozen_target["600001.SH"] == 0
        assert cycle.schedule[0]["trade_date"] == "20261008"
        assert cycle.status == "PENDING_APPROVAL"
        sell = s.query(OmsOrder).one()
        assert sell.symbol == "600001.SH" and sell.side == "SELL" and sell.quantity == 100


def test_cli_dry_run_does_not_create_missing_database(tmp_path):
    from scripts.oms_ops import main
    path = tmp_path / "not-created.db"
    with pytest.raises(SystemExit, match="does not exist"):
        main(["status", "--account", "shared"], settings=Settings(_env_file=None, db_url=f"sqlite:///{path}"))
    assert not path.exists()
