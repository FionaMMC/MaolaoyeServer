"""Run only inside a fresh /opt/qmt-server/private/research-runs/hydra-policy-* dir.

Writes summary.json plus per-run cycle/NAV CSVs; raw market data never leaves
the server. Inputs come from the same frozen loader as the 2026-10-02 replay.
"""
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import sys

ROOT = Path('/opt/qmt-server/private/research-runs/etf-final-20260928/reports')
sys.path.insert(0, str(ROOT / 'execution_experiment_20260927'))
import backtest as source  # noqa: E402  load_inputs(): raw bars, weights, corporate actions

from policy_replay import Policy, cycle_stats, next_pair, run  # noqa: E402

DIFF = (('518880.SH', 100.), ('513100.SH', 150.), ('513500.SH', 150.))
CANDIDATE = dict(lot='nearest', size_factor=1.001, buy_bps=DIFF, touch=True)

POLICIES = [
    Policy('P0_legacy'),
    Policy('L_nearest', lot='nearest', size_factor=1.001),
    Policy('L_nearest_legacy_factor', lot='nearest'),
    Policy('B_diff', buy_bps=DIFF),
    Policy('B_100', default_buy_bps=100.),
    Policy('B_200', default_buy_bps=200.),
    Policy('T_touch', touch=True),
    Policy('A_rolling', anchor='rolling'),
    Policy('W_1', window=1),
    Policy('W_5', window=5),
    Policy('C1', **CANDIDATE),
    Policy('C1_open_only', **{**CANDIDATE, 'touch': False}),
    Policy('C1_factor_1.003', **{**CANDIDATE, 'size_factor': 1.003}),
    Policy('C1_legacy_factor', **{**CANDIDATE, 'size_factor': 1.006005}),
    Policy('C1_replan', **CANDIDATE, sizing='replan'),
    Policy('C1_retarget', **CANDIDATE, sizing='retarget'),
    Policy('C1_rolling', **CANDIDATE, anchor='rolling'),
    Policy('C1_w1', **CANDIDATE, window=1),
    Policy('C1_w2', **CANDIDATE, window=2),
    Policy('C1_w5', **CANDIDATE, window=5),
    Policy('C1_reserve1', **CANDIDATE, reserve=.01),
    Policy('C1_reserve2', **CANDIDATE, reserve=.02),
]
STRESS = ['P0_legacy', 'C1', 'C1_factor_1.003', 'C1_replan']
SCENARIOS = [
    dict(name='base', capital=200000, slip_bps=5., participation=.01, terminal_delay=0),
    dict(name='slip25', capital=200000, slip_bps=25., participation=.01, terminal_delay=0),
    dict(name='capacity01', capital=200000, slip_bps=5., participation=.001, terminal_delay=0),
    dict(name='terminal_delay1', capital=200000, slip_bps=5., participation=.01, terminal_delay=1),
    dict(name='capital100k', capital=100000, slip_bps=5., participation=.01, terminal_delay=0),
    dict(name='capital1m', capital=1000000, slip_bps=5., participation=.01, terminal_delay=0),
]


def horizon_filter(weights, close, sessions):
    keep = []
    for d in weights.index:
        _, b = next_pair(close.index, d)
        if b is not None and close.index.get_loc(b) + sessions - 1 < len(close):
            keep.append(d)
    return weights.loc[keep]


def main():
    assert str(Path.cwd()).startswith('/opt/qmt-server/private/research-runs/hydra-policy-')
    weights, daily, close, actions, validation = source.load_inputs()
    # Same signals for every policy: the longest window (5) sets the horizon.
    common = horizon_filter(weights, close, 5)
    legacy = horizon_filter(weights, close, 3)
    out = dict(remote_research_dir=str(Path.cwd()), validation=validation,
               source_sha256={n: hashlib.sha256(Path(n).read_bytes()).hexdigest()
                              for n in ('policy_replay.py', 'run_policy_grid.py', 'allocation_code.py')},
               loader=str(ROOT / 'execution_experiment_20260927/backtest.py'),
               signals=dict(common=[str(d.date()) for d in common.index],
                            legacy=[str(d.date()) for d in legacy.index]),
               policies=[asdict(p) for p in POLICIES], panels=[], regression=[])

    # Regression: the fork must reproduce the 2026-10-02 'fixed' arm on its sample.
    for period, subset in (('full', legacy), ('recent', legacy.loc['2024-10-01':])):
        ideal, _, _, _, _ = run(subset, daily, close, actions, arm='signal_ideal', capital=200000)
        summary, _, frame, _, _ = run(subset, daily, close, actions, Policy('P0_legacy'), capital=200000)
        stats = cycle_stats(frame, skip_first=False)
        out['regression'].append(dict(period=period, cagr=summary['cagr'],
                                      drag_pp=(ideal['cagr'] - summary['cagr']) * 100,
                                      total_uw_mean=stats['total_uw_mean'], total_uw_max=stats['total_uw_max'],
                                      total_uw_cycles_above_2pct=stats['total_uw_cycles_above_2pct'],
                                      cycles=stats['cycles']))
    print(json.dumps({'regression': out['regression']}), flush=True)

    by_name = {p.name: p for p in POLICIES}
    for period, subset in (('full', common), ('recent', common.loc['2024-10-01':])):
        for scenario in SCENARIOS:
            names = [p.name for p in POLICIES] if scenario['name'] == 'base' else STRESS
            params = {k: v for k, v in scenario.items() if k != 'name'}
            ideal_params = dict(capital=scenario['capital'])
            signal_ideal, signal_hist, _, _, _ = run(subset, daily, close, actions, arm='signal_ideal', **ideal_params)
            pair_ideal, _, _, _, _ = run(subset, daily, close, actions, arm='pair_ideal', **ideal_params)
            arms = []
            for name in names:
                summary, hist, frame, fills, events = run(subset, daily, close, actions, by_name[name], **params)
                stem = f"{period}_{scenario['name']}_{name}"
                frame.to_csv(stem + '_cycles.csv', index=False)
                hist.to_csv(stem + '_nav.csv')
                events.to_csv(stem + '_events.csv', index=False)
                ratios = (hist.nav / signal_hist.nav).resample('ME').last().pct_change().dropna()
                summary.update(
                    drag_pp=(signal_ideal['cagr'] - summary['cagr']) * 100,
                    drag_vs_pair_pp=(pair_ideal['cagr'] - summary['cagr']) * 100,
                    tracking_error_annual=float(ratios.std(ddof=1) * 12 ** .5),
                    worst_month_relative=float(ratios.min()),
                    all_cycles=cycle_stats(frame, skip_first=False),
                    rebalances=cycle_stats(frame, skip_first=period == 'full'))
                arms.append(summary)
            out['panels'].append(dict(period=period, scenario=scenario,
                                      signal_ideal=signal_ideal, pair_ideal=pair_ideal, arms=arms))
            print(json.dumps({'progress': f"{period}_{scenario['name']}"}), flush=True)
    Path('summary.json').write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


if __name__ == '__main__':
    main()
