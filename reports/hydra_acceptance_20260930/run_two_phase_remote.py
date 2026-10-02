"""Read the frozen remote ETF archive, write details remotely, emit summaries."""
import hashlib
import importlib.util
import json
from pathlib import Path

from two_phase_replay import run

archive = Path('/opt/qmt-server/private/research-data/etf-execution-20260928')
source = archive/'reports/execution_experiment_20260927/backtest.py'
spec = importlib.util.spec_from_file_location('frozen_inputs', source)
module = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = module
spec.loader.exec_module(module)
weights, bars, close, actions, validation = module.load_inputs()
out = {'input_loader_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
       'raw_to_hfq_validation': validation, 'results': [],
       'protocol': {
           'signal_availability': 'after signal-date close; sell strictly later',
           'execution': 'sell official close; buy adjacent-natural-day open plus adverse slip',
           'calendar': 'dates with archived prices; no business-weekday approximation',
           'targets': 'frozen reconstructed Hydra monthly weights; no refit',
           'capacity': 'ex-post 1% daily volume, source volume in lots of 100; not auction liquidity',
           'commission': '1bp, minimum CNY5 per symbol fill; ETF model, no stock stamp duty',
           'costs': 'buy slippage 5/25/50bp; theoretical official-close sells, no sell impact',
           'sample': 'retrospective scenario replay, not live or prospective forward test',
       }}
for capital in (200000, 1000000):
    for slip in (5, 25, 50):
        for policy in ('slippage_at_open', 'previous_close_limit'):
            summary, nav, cycles, fills = run(weights, bars, close, actions,
                capital=capital, slip_bps=slip, buy_policy=policy)
            key = f'{policy}_{capital}_{slip}'
            nav.to_csv(f'{key}_nav.csv')
            cycles.to_csv(f'{key}_cycles.csv', index=False)
            fills.to_csv(f'{key}_fills.csv', index=False)
            out['results'].append(summary)
out['remote_research_dir'] = str(Path.cwd())
Path('two_phase_summary.json').write_text(json.dumps(out, ensure_ascii=False, indent=2))
print(json.dumps(out, ensure_ascii=False))
