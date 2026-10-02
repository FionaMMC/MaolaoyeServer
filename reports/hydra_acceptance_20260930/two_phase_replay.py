"""Offline ETF close-sell / adjacent-session open-buy accounting model.

No order API or deployment entrypoint. Signals are assumed known AFTER their
dated close: the sell session must be strictly later to avoid same-close bias.
"""
from collections import defaultdict
import math

import numpy as np
import pandas as pd


def next_pair(dates, signal_date):
    known = set(pd.DatetimeIndex(dates))
    return next(((d, d + pd.Timedelta(days=1)) for d in sorted(known)
                 if d > signal_date and d + pd.Timedelta(days=1) in known), (None, None))


def fee(gross):
    return max(5.0, gross * .0001) if gross else 0.0


def run(weights, bars, close, actions, *, capital=200000.0, slip_bps=5.0,
        buy_policy='slippage_at_open', participation=.01, volume_unit=100):
    if buy_policy not in ('slippage_at_open', 'previous_close_limit'):
        raise ValueError('unknown buy policy')
    if not 0 <= slip_bps < 10000 or not 0 < participation <= 1 or capital <= 0:
        raise ValueError('invalid scenario')
    if not weights.index.is_unique or not weights.index.is_monotonic_increasing:
        raise ValueError('targets must be unique and chronological')
    if (weights < 0).any().any() or not np.isfinite(weights.to_numpy()).all():
        raise ValueError('invalid target weights')
    if (weights.sum(axis=1) > 1 + 1e-9).any():
        raise ValueError('target exceeds capital')
    dates = close.loc[weights.index.min():].index
    symbols = list(weights.columns)
    quantities = dict.fromkeys(symbols, 0)
    cash = float(capital)
    pending = None
    rights, receivables = {}, []
    by_ex, by_record = defaultdict(list), defaultdict(list)
    history, fills, cycles = [], [], []
    for action in actions:
        by_ex[action['ex_date']].append(action)
        if action['record_date'] is not None:
            by_record[action['record_date']].append(action)
    total_fees = 0.0
    clipped = 0

    def finish(reason):
        if pending:
            cycles.append({k: pending[k] for k in
                           ('signal_date', 'sell_date', 'buy_date', 'intended', 'filled')}
                          | {'reason': reason})

    for date in dates:
        for a in by_ex[date]:
            symbol = a['symbol']
            if a['factor'] != 1:
                quantities[symbol] = round(quantities[symbol] * a['factor'])
                if pending:
                    pending['target'][symbol] = round(pending['target'][symbol] * a['factor'])
                    pending['reference'][symbol] /= a['factor']
                    pending['sell_marks'][symbol] /= a['factor']
            if a['cash']:
                receivables.append([a['pay_date'], rights.get((symbol, a['ex_date']), 0) * a['cash']])
                if pending:
                    pending['sell_marks'][symbol] -= a['cash']
        for item in receivables:
            if item[0] <= date:
                cash += item[1]
                item[1] = 0.0
        if pending and date in (pending['sell_date'], pending['buy_date']):
            selling = date == pending['sell_date']
            side = -1 if selling else 1
            for symbol in symbols:
                delta = pending['target'][symbol] - quantities[symbol]
                if not delta or int(np.sign(delta)) != side:
                    continue
                bar = bars.get((date, symbol))
                if (bar is None or bar.get('suspendFlag', 0) or bar['volume'] <= 0
                        or bar['high'] <= bar['low']):
                    continue
                price = float(bar['close']) if selling else (
                    math.ceil(float(bar['open']) * (1 + slip_bps / 10000) / .001 - 1e-8) * .001)
                if not math.isfinite(price) or price <= 0:
                    raise ValueError('invalid execution price')
                if not selling and buy_policy == 'previous_close_limit':
                    limit = math.floor(pending['sell_marks'][symbol] / .001 + 1e-8) * .001
                    if price > limit + 1e-10:
                        continue
                cap = int(float(bar['volume']) * volume_unit * participation) // 100 * 100
                quantity = min(abs(delta), cap) // 100 * 100
                if not selling:
                    requested = quantity
                    quantity = min(quantity, max(0, int((cash - 5) / price) // 100 * 100))
                    while quantity and quantity * price + fee(quantity * price) > cash + 1e-8:
                        quantity -= 100
                    clipped += quantity < requested
                if quantity <= 0:
                    continue
                cost = fee(quantity * price)
                cash -= side * quantity * price + cost
                quantities[symbol] += side * quantity
                total_fees += cost
                pending['filled'] += quantity * pending['reference'][symbol]
                assert cash >= -1e-6 and quantities[symbol] >= 0
                fills.append({'date': date, 'signal_date': pending['signal_date'],
                              'side': 'SELL' if selling else 'BUY', 'symbol': symbol,
                              'quantity': quantity, 'price': price, 'fee': cost,
                              'cash_after': cash})
            if selling:
                pending['sell_marks'] = {s: float(close.loc[date, s]) for s in symbols}
            else:
                finish('window_complete')
                pending = None
        marks = close.loc[date]
        value = sum(quantities[s] * marks[s] for s in symbols if quantities[s])
        nav = cash + value + sum(r[1] for r in receivables)
        if not math.isfinite(nav) or nav <= 0:
            raise ValueError('nonpositive or missing portfolio valuation')
        history.append({'date': date, 'nav': nav, 'cash': cash,
                        'marked_holdings': value, 'receivable': sum(r[1] for r in receivables)})
        for a in by_record[date]:
            rights[(a['symbol'], a['ex_date'])] = quantities[a['symbol']]
        if date in weights.index:
            finish('superseded_by_new_target')
            sell_date, buy_date = next_pair(dates, date)
            reference = {s: float(marks[s]) for s in symbols}
            for s, price in reference.items():
                if not math.isfinite(price) or price <= 0:
                    if quantities[s] or weights.loc[date, s] > 0:
                        raise ValueError('missing required signal-date mark')
                    # Unlisted, zero-weight, unheld symbols contribute no
                    # target notional. This sentinel is never a fill price.
                    reference[s] = 0.0
            target = {s: (int(nav * .99 * weights.loc[date, s] / reference[s]) // 100 * 100
                          if weights.loc[date, s] > 0 else 0)
                      for s in symbols}
            pending = {'signal_date': date, 'sell_date': sell_date, 'buy_date': buy_date,
                       'reference': reference, 'sell_marks': reference.copy(), 'target': target,
                       'intended': sum(abs(target[s] - quantities[s]) * reference[s] for s in symbols),
                       'filled': 0.0}
    finish('insufficient_calendar_or_price_horizon')
    hist = pd.DataFrame(history).set_index('date')
    cyc = pd.DataFrame(cycles)
    nav = hist.nav
    # Include initial capital in the high-water mark; first-period fees count.
    augmented = pd.Series([capital, *nav.tolist()])
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    completed = cyc[cyc.reason == 'window_complete']
    intended = completed.intended.sum()
    summary = {'policy': buy_policy, 'capital': capital, 'slip_bps': slip_bps,
               'daily_participation': participation, 'start': str(nav.index[0].date()),
               'end': str(nav.index[-1].date()), 'sessions': len(nav), 'targets': len(cyc),
               'completed_windows': len(completed),
               'unexecuted_windows': int((cyc.reason != 'window_complete').sum()),
               'total_return': float(nav.iloc[-1] / capital - 1),
               'cagr': float((nav.iloc[-1] / capital) ** (1 / years) - 1) if years else None,
               'max_drawdown': float((augmented / augmented.cummax() - 1).min()),
               'modeled_notional_completion': float(completed.filled.sum() / intended) if intended else None,
               'fees_pct_initial': total_fees / capital, 'cash_clipped_fills': int(clipped),
               'minimum_cash': float(hist.cash.min()), 'fills': len(fills)}
    return summary, hist, cyc, pd.DataFrame(fills)
