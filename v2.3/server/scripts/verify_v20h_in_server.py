"""验证 vendored V20H 代码在 server venv 里能跑通且数字一致。

目标:
  α (excess_ann):  +12.50% (allow ±0.5%)
  Sharpe:           1.12   (allow ±0.05)
  Max DD:          -17.22% (allow ±1.0%)
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

# 让 plugins/ 可被 import
_HERE = Path(__file__).resolve().parent
_SERVER_ROOT = _HERE.parent
sys.path.insert(0, str(_SERVER_ROOT))

from plugins.v20h.strategy import StrategyConfig
from plugins.v20h.data_loader import DataLoader
from plugins.v20h.backtest import run_backtest


EXPECTED = {
    "excess_ann": (0.1250, 0.005),
    "sharpe":     (1.12,   0.05),
    "max_dd":     (-0.1722, 0.010),
}


def main() -> int:
    print("=" * 60)
    print("  V20H Verify (in v2.3 server venv)")
    print("=" * 60)

    cfg_path = _SERVER_ROOT / "plugins" / "v20h" / "config.yaml"
    with cfg_path.open() as f:
        cfg = StrategyConfig(**yaml.safe_load(f))

    data_dir = _SERVER_ROOT / "plugins" / "v20h" / "data"
    loader = DataLoader(data_dir)
    loader.verify()
    data = loader.load_all()

    print(f"\n  运行 1000 万资金回测...")
    result = run_backtest(data, cfg)
    stats = result["stats"]

    print(f"\n  α:        {stats['excess_ann']:>+7.2%}")
    print(f"  Sharpe:   {stats['sharpe']:>7.2f}")
    print(f"  Max DD:   {stats['max_dd']:>+7.2%}")

    all_pass = True
    for metric, (expected, tol) in EXPECTED.items():
        actual = stats[metric]
        diff = abs(actual - expected)
        ok = diff <= tol
        sym = "✅" if ok else "❌"
        print(f"  {sym} {metric}: expected {expected:+.4f}, got {actual:+.4f}")
        if not ok:
            all_pass = False

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
