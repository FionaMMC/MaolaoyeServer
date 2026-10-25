"""V713 stock leg: rule expansion, derived targets, liquidation and monthly state."""
import json

import pandas as pd
import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState
from app.oms.cycles import CycleConflict, CycleService
from app.oms.instruments import InstrumentSpec, instrument_to_dict
from app.oms.ledger import OrderLedger
from app.oms.models import OmsCycle, OmsOrder
from app.oms.planner import Policy, policy_to_dict
from app.oms.v713 import build_publication, validate_v713_policy
from app.oms.v713_stock_leg import expand_policy, instrument_rule
from app.services.reconcile import ReconcileService
from app.services.settlement import SettlementService
from plugins.v713_relay import basket_hash

CAL = ["20260831", "20260901", "20260929", "20260930", "20261008", "20261009", "20261012",
       "20261013", "20261014", "20261015", "20261016", "20261019", "20261020", "20261021"]
NOW = "2026-10-07T20:00:00+08:00"
ALIAS = "shared"
INSTANCE = "live_v713_stockleg"
CLOSES = {"600001.SH": 10.0, "600002.SH": 20.0, "511260.SH": 120.0}


def _template():
    board = instrument_to_dict(InstrumentSpec("stock", .01, commission_rate=.0002, stamp_duty_sell=.0005))
    star = instrument_to_dict(InstrumentSpec("stock", .01, buy_lot=1, min_buy_quantity=200, sell_lot=1,
                                             commission_rate=.0002, stamp_duty_sell=.0005))
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
