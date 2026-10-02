"""Offline lower-bound diagnostic; no broker imports, network or order output.

Enumerates floor/ceiling target lots. Ignores fees, slippage and existing order
obligations, so passing is NOT an execution approval; failing proves even this
optimistic rounding choice cannot meet the specified total allocation error.
"""
from itertools import product
import math


def best_lot_fit(nav, weights, prices, cash_buffer=0., lot=100):
    if not math.isfinite(nav) or nav<=0 or not 0<=cash_buffer<1 or lot<=0:
        raise ValueError('invalid capital/buffer/lot')
    if any(not math.isfinite(w) or w<0 for w in weights.values()) or abs(sum(weights.values())-1)>1e-7:
        raise ValueError('weights must be finite, nonnegative and sum to 1')
    symbols=sorted(weights)
    choices=[]
    for s in symbols:
        if s not in prices or not math.isfinite(prices[s]) or prices[s]<=0:
            if weights[s]==0:
                choices.append((0,))
                continue
            raise ValueError('missing positive price')
        desired=nav*(1-cash_buffer)*weights[s]/(prices[s]*lot)
        choices.append(tuple(sorted({math.floor(desired),math.ceil(desired)})))
    best=None
    for lots in product(*choices):
        values={s:n*lot*prices.get(s,0.) for s,n in zip(symbols,lots)}
        cash=nav-sum(values.values())
        if cash<-1e-7:
            continue
        error=.5*(sum(abs(values[s]/nav-(1-cash_buffer)*weights[s]) for s in symbols)+abs(cash/nav-cash_buffer))
        candidate=(error,-cash,lots)
        if best is None or candidate<best[0]:
            best=(candidate,values,cash)
    if best is None:
        raise ValueError('no feasible integer portfolio')
    return dict(minimum_allocation_error=best[0][0],cash_weight=best[2]/nav,
                largest_lot_weight=max(prices.get(s,0)*lot/nav for s in symbols),
                diagnostic_only=True)
