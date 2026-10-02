"""Follow-up grid: sell-side guard width found to block de-risking in grid 1.

Same loader, sample and output layout as run_policy_grid.py; writes summary2.json.
"""
from dataclasses import asdict
from pathlib import Path
import hashlib
import json

from policy_replay import Policy, run
import run_policy_grid as base

C1 = dict(base.CANDIDATE)
C2 = dict(C1, sell_bps=200.)
POLICIES = [
    Policy('P0_legacy'),
    Policy('P0_sell200', sell_bps=200.),
    Policy('C1', **C1),
    Policy('C1_sell100', **dict(C1, sell_bps=100.)),
    Policy('C2', **C2),
    Policy('C1_sell300', **dict(C1, sell_bps=300.)),
    Policy('C1_sell1000', **dict(C1, sell_bps=1000.)),
    Policy('C1_sell_rolling200', **dict(C1, sell_bps=200., anchor='rolling')),
    Policy('C2_factor_1.003', **dict(C2, size_factor=1.003)),
    Policy('C2_legacy_factor', **dict(C2, size_factor=1.006005)),
    Policy('C2_replan', **C2, sizing='replan'),
    Policy('C2_retarget', **C2, sizing='retarget'),
    Policy('C2_w2', **C2, window=2),
    Policy('C2_w5', **C2, window=5),
    Policy('C2_reserve1', **C2, reserve=.01),
]
STRESS = ['P0_legacy', 'C1', 'C2', 'C2_factor_1.003']


def run_grid(policies, stress, tag, output):
    assert str(Path.cwd()).startswith('/opt/qmt-server/private/research-runs/hydra-policy-')
    weights, daily, close, actions, validation = base.source.load_inputs()
    common = base.horizon_filter(weights, close, 5)
    out = dict(source_sha256={n: hashlib.sha256(Path(n).read_bytes()).hexdigest()
                              for n in ('policy_replay.py', 'run_policy_grid.py', 'run_policy_grid2.py',
                                        'run_policy_grid3.py', 'allocation_code.py') if Path(n).exists()},
               policies=[asdict(p) for p in policies], panels=[])
    by_name = {p.name: p for p in policies}
    for period, subset in (('full', common), ('recent', common.loc['2024-10-01':])):
        for scenario in base.SCENARIOS:
            names = [p.name for p in policies] if scenario['name'] == 'base' else stress
            params = {k: v for k, v in scenario.items() if k != 'name'}
            signal_ideal, signal_hist, _, _, _ = run(subset, daily, close, actions, arm='signal_ideal',
                                                     capital=scenario['capital'])
            pair_ideal, _, _, _, _ = run(subset, daily, close, actions, arm='pair_ideal', capital=scenario['capital'])
            arms = []
            for name in names:
                summary, hist, frame, fills, events = run(subset, daily, close, actions, by_name[name], **params)
                stem = f"{tag}_{period}_{scenario['name']}_{name}"
                frame.to_csv(stem + '_cycles.csv', index=False)
                hist.to_csv(stem + '_nav.csv')
                events.to_csv(stem + '_events.csv', index=False)
                fills.to_csv(stem + '_fills.csv', index=False)
                ratios = (hist.nav / signal_hist.nav).resample('ME').last().pct_change().dropna()
                summary.update(
                    drag_pp=(signal_ideal['cagr'] - summary['cagr']) * 100,
                    drag_vs_pair_pp=(pair_ideal['cagr'] - summary['cagr']) * 100,
                    tracking_error_annual=float(ratios.std(ddof=1) * 12 ** .5),
                    worst_month_relative=float(ratios.min()),
                    all_cycles=base.cycle_stats(frame, skip_first=False),
                    rebalances=base.cycle_stats(frame, skip_first=period == 'full'))
                arms.append(summary)
            out['panels'].append(dict(period=period, scenario=scenario,
                                      signal_ideal=signal_ideal, pair_ideal=pair_ideal, arms=arms))
            print(json.dumps({'progress': f"{period}_{scenario['name']}"}), flush=True)
    Path(output).write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


if __name__ == '__main__':
    run_grid(POLICIES, STRESS, 'g2', 'summary2.json')
