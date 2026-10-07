"""Read-only: C3 fill rates by signal->sell gap type, first attempt vs end of window (10/8 question).

Value = shares x signal-day close (the reference price the cycle records use), so first-attempt
and final rates share one denominator. Needs gap_analysis_20261008.json from gap_analysis.py.
"""
import json
from pathlib import Path

import pandas as pd

import run_policy_grid as base

assert str(Path.cwd()).startswith('/opt/qmt-server/private/research-runs/hydra-policy-')
_, _, close, _, _ = base.source.load_inputs()
cycles = pd.read_csv('g3_full_base_C3_50_200_cycles.csv').set_index('signal')
fills = pd.read_csv('g3_full_base_C3_50_200_fills.csv')
fills['date'] = fills['date'].astype(str)
gaps = pd.DataFrame(json.loads(Path('gap_analysis_20261008.json').read_text())['all_cycles']).set_index('signal')

rows = []
for signal, g in gaps.iterrows():
    c = cycles.loc[signal]
    ref = close.loc[pd.Timestamp(signal)]
    sell_day, buy_day = f"{g.sell[:4]}-{g.sell[4:6]}-{g.sell[6:]}" if '-' not in g.sell else g.sell, g.buy
    window = fills[(fills.date >= sell_day) & (fills.date <= str(c.end))]

    def value(frame):
        return float(sum(q * float(ref[s]) for s, q in zip(frame.symbol, frame.quantity)))

    rows.append(dict(
        signal=signal, kind=g.kind,
        sell_intended=float(c.sell_intended), sell_final=float(c.sell_filled),
        sell_first=value(window[(window.direction == 'SELL') & (window.date == sell_day)]),
        buy_intended=float(c.buy_intended), buy_final=float(c.buy_filled),
        buy_first=value(window[(window.direction == 'BUY') & (window.date == buy_day)])))
df = pd.DataFrame(rows)


def rates(frame):
    out = {'cycles': len(frame)}
    for side in ('sell', 'buy'):
        intended = frame[f'{side}_intended'].sum()
        out[f'{side}_first'] = frame[f'{side}_first'].sum() / intended if intended else None
        out[f'{side}_final'] = frame[f'{side}_final'].sum() / intended if intended else None
    return out


result = {}
for label, subset in (('excl_initial_build', df[df.signal > '2019-12-05']), ('from_2020_03', df[df.signal >= '2020-03-01']),
                      ('recent_2024_10', df[df.signal >= '2024-10-01'])):
    result[label] = {kind: rates(g) for kind, g in subset.groupby('kind')}
    result[label]['all'] = rates(subset)
result['national_day'] = df[df.signal.str[5:7] == '09'].round(2).to_dict('records')
Path('fill_rates_20261008.json').write_text(json.dumps(result, indent=2, default=float))
print(json.dumps(result, indent=1, default=lambda x: round(float(x), 4)))
