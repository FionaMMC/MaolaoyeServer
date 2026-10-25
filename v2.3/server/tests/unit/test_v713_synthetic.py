"""Synthetic V713 ledger over the live stock leg and live Hydra (read-only)."""
import json
from pathlib import Path

import pytest

from app.services.v713_synthetic import SyntheticPolicy, load_synthetic_policy, rebalance, unit_nav

POLICY_FILE = Path(__file__).resolve().parents[2] / "plugins" / "v713" / "synthetic_policy_v1.json"
POLICY = SyntheticPolicy(policy_id="V713_SYNTHETIC_TEST", stock_instance="live_v713_stockleg",
                         hydra_instance="live_hydra_v481_rb", label="test")


def test_unit_nav_chains_flow_adjusted_returns():
    units = unit_nav([("20261202", .1), ("20261201", None), ("20261203", -.5)])
    assert units == pytest.approx({"20261201": 1.0, "20261202": 1.1, "20261203": .55})
    with pytest.raises(ValueError, match="20261202"):
        unit_nav([("20261201", None), ("20261202", None)])


def test_rebalance_matches_the_design_example():
    # TOP50 -> T1 at 400k: virtual stock sale and virtual Hydra purchase, 20bp each.
    values, cash, cost, traded = rebalance([400_000.0, 0.0], 0.0, .5, (.002, .002))
    assert values == pytest.approx([200_000.0, 199_201.596806])
    assert cash == pytest.approx(0.0, abs=1e-9)
    assert cost == pytest.approx(798.403194)
    assert traded == pytest.approx(399_201.596806)


def test_real_leg_trades_are_not_charged_again():
    # AUX exit: the Hydra sale is virtual, the stock rebuild is real and already in its NAV.
    values, _, cost, _ = rebalance([0.0, 400_000.0], 0.0, 1.0, (0.0, .002))
    assert cost == pytest.approx(800.0)
    assert values[0] == pytest.approx(399_200.0)


def test_frozen_policy_file_loads_and_unversioned_policy_is_rejected(tmp_path):
    policy = load_synthetic_policy(POLICY_FILE)
    assert (policy.base_value, policy.cost_bps, policy.primary_bps) == (400_000.0, (0.0, 20.0, 50.0), 20.0)
    bad = json.loads(POLICY_FILE.read_text(encoding="utf-8")) | {"policy_id": "synthetic"}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="V713_SYNTHETIC_"):
        load_synthetic_policy(path)
