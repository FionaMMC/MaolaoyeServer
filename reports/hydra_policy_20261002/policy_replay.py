"""Two-phase execution-policy replay. Research only, never an order router.

Fork of reports/hydra_fill_replay_20261002/continuous_replay.py. Same calendar,
corporate-action, fee and capacity conventions (sell at the reference close,
buy at the next natural-day open, all state carried across months). Adds the
knobs the October design has to choose, and the agreed metric split:

* lot_gap  - lot target vs ideal weights at the signal close (disclosed only)
* exec_uw  - lot-target buys still missing at cycle end, divided by NAV (gate 2%)

Every unfinished share at cycle end is attributed to the last reason recorded
for that symbol and side, so underweight can be explained instead of guessed.
"""
from collections import defaultdict
from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

from allocation_code import allocate_buys

TICK = .001
LOT = 100


def fee(gross):
    return max(5., gross * .0001) if gross else 0.


@dataclass(frozen=True)
class Policy:
    name: str
    lot: str = 'floor'              # floor | nearest
    size_factor: float = 1.006005   # target divisor kept for price drift and fees
    buy_bps: tuple = ()             # ((symbol, bps), ...) per-symbol buy bands
    default_buy_bps: float = 50.
    sell_bps: float = 50.
    anchor: str = 'fixed'           # fixed | rolling
    touch: bool = False             # buy order rests intraday after the open auction
    window: int = 3
    sizing: str = 'fixed'           # fixed | replan | retarget
    reserve: float = 0.
    sell_schedule: tuple = ()       # per sell attempt bps, last value repeats; overrides sell_bps

    def buy_band(self, symbol):
        return dict(self.buy_bps).get(symbol, self.default_buy_bps)

    def sell_band(self, attempt):
        if not self.sell_schedule:
            return self.sell_bps
        return self.sell_schedule[min(attempt, len(self.sell_schedule) - 1)]


def next_pair(dates, signal):
    return next(((a, b) for a, b in zip(dates[:-1], dates[1:]) if a > signal and (b - a).days == 1),
                (None, None))


def lot_target(nav, weights, prices, policy):
    """Whole-lot target. ``nearest`` rounds up a lot only while it cuts the error
    and the whole basket, grossed up by ``size_factor``, still fits the budget."""
    investable = nav * (1 - policy.reserve)
    target = {s: (math.floor(investable * w / prices[s] / policy.size_factor / LOT) * LOT if w > 0 else 0)
              for s, w in weights.items()}
    if policy.lot == 'floor':
        return target
    if policy.lot != 'nearest':
        raise ValueError(policy.lot)
    spent = sum(q * prices[s] for s, q in target.items() if q) * policy.size_factor
    while True:
        best, best_gain = None, 0.
        for s in sorted(weights):
            if weights[s] <= 0:
                continue
            gap = investable * weights[s] - target[s] * prices[s]
            step = LOT * prices[s]
            gain = gap - abs(gap - step)
            if gain > best_gain + 1e-9 and spent + step * policy.size_factor <= investable + 1e-9:
                best, best_gain = s, gain
        if best is None:
            return target
        target[best] += LOT
        spent += LOT * prices[best] * policy.size_factor


def buy_limit(anchor, bps):
    return math.floor(anchor * (1 + bps / 1e4) / TICK + 1e-8) * TICK


def sell_limit(anchor, bps):
    return math.ceil(anchor * (1 - bps / 1e4) / TICK - 1e-8) * TICK


def auction_fill(bar, side, limit, slip_bps, touch):
    """Sell: closing auction only. Buy: opening auction, optionally resting until
    the 14:55 cancel (daily low touch fills at the limit)."""
    if bar is None or bar['volume'] <= 0 or bar.get('suspendFlag', 0) or bar['high'] <= bar['low']:
        return None, 'SUSPENDED'
    reference = bar['open'] if side > 0 else bar['close']
    arrival = reference * (1 + side * slip_bps / 1e4)
    price = (math.ceil(arrival / TICK - 1e-8) if side > 0 else math.floor(arrival / TICK + 1e-8)) * TICK
    if side * (price - limit) <= 1e-8:
        return price, None
    if touch and side > 0 and bar['low'] < limit - TICK / 2:
        return limit, None
    return None, 'PRICE_GUARD'


def run(weights, daily, close, actions, policy=None, *, arm='policy', capital=200000.,
        slip_bps=5., participation=.01, terminal_delay=0):
    if arm not in ('signal_ideal', 'pair_ideal', 'policy'):
        raise ValueError(arm)
    ideal = arm != 'policy'
    policy = policy or Policy('legacy')
    window = policy.window
    dates = close.loc[weights.index.min():].index
    positions = dict.fromkeys(weights.columns, 0)
    state = {'cash': float(capital), 'fees': 0.}
    rights, receivables = {}, []
    ex, records = defaultdict(list), defaultdict(list)
    for a in actions:
        ex[a['ex_date']].append(a)
        if a['record_date'] is not None:
            records[a['record_date']].append(a)
    pending = None
    history, cycles, fills, events = [], [], [], []

    def marks(date):
        return {s: float(v) for s, v in close.loc[date].items() if math.isfinite(v) and v > 0}

    def nav_at(date):
        prices = marks(date)
        return (state['cash'] + sum(q * prices[s] for s, q in positions.items() if q)
                + sum(r[1] for r in receivables))

    def attempt(symbol, side):
        # Unfilled-share reasons of the latest session only; earlier sessions are
        # superseded by later attempts on the same residual.
        pending['reason'][symbol, side] = defaultdict(float)

    def note(date, symbol, side, reason, quantity):
        pending['reason'].setdefault((symbol, side), defaultdict(float))[reason] += quantity
        events.append(dict(date=str(date.date()), symbol=symbol, side=side, reason=reason, unfilled=quantity))

    def book(date, symbol, side, quantity, price, phase):
        cost = fee(quantity * price)
        sign = 1 if side == 'BUY' else -1
        positions[symbol] += sign * quantity
        state['cash'] -= sign * quantity * price + cost
        state['fees'] += cost
        pending['done'][symbol] += quantity
        fills.append(dict(date=str(date.date()), phase=phase, symbol=symbol, direction=side,
                          quantity=quantity, price=price, fee=cost))
        assert state['cash'] >= -1e-6 and positions[symbol] >= 0

    def finish(date, reason):
        prices = marks(date)
        nav = nav_at(date)
        frozen = pending['frozen']
        w = pending['weights']
        causes = defaultdict(float)
        for s, q in frozen.items():
            remaining = q - positions.get(s, 0)
            if abs(remaining) <= 1e-8:
                continue
            side = 'BUY' if remaining > 0 else 'SELL'
            value = abs(remaining) * prices.get(s, 0) / nav
            reasons = pending['reason'].get((s, side))
            if reasons is None:
                causes[side + '_NO_SESSION'] += value
            elif not sum(reasons.values()):
                causes[side + '_TARGET_CHANGED'] += value
            else:
                total = sum(reasons.values())
                for key, quantity in reasons.items():
                    causes[side + '_' + key] += value * quantity / total
        requested = pending['original']
        done = pending['done']
        cycles.append(dict(
            signal=str(pending['signal'].date()), end=str(date.date()), reason=reason,
            lot_gap=pending['lot_gap'],
            exec_underweight=sum(max(0, q - positions.get(s, 0)) * prices.get(s, 0) for s, q in frozen.items()) / nav,
            exec_overweight=sum(max(0, positions.get(s, 0) - q) * prices.get(s, 0) for s, q in frozen.items()) / nav,
            underweight=sum(max(0, (1 - policy.reserve) * float(x) - positions.get(s, 0) * prices.get(s, 0) / nav)
                            for s, x in w.items()),
            uninvested_vs_full_target=sum(max(0, float(x) - positions.get(s, 0) * prices.get(s, 0) / nav)
                                          for s, x in w.items()),
            buy_intended=sum(o['quantity'] * o['reference_price'] for o in requested if o['direction'] == 'BUY'),
            buy_filled=sum(min(done.get(o['symbol'], 0), o['quantity']) * o['reference_price']
                           for o in requested if o['direction'] == 'BUY'),
            sell_intended=sum(o['quantity'] * o['reference_price'] for o in requested if o['direction'] == 'SELL'),
            sell_filled=sum(min(done.get(o['symbol'], 0), o['quantity']) * o['reference_price']
                            for o in requested if o['direction'] == 'SELL'),
            **{'cause_' + k: v for k, v in causes.items()}))

    def ideal_phase(date, selling):
        side = 'SELL' if selling else 'BUY'
        field = 'close' if selling else 'open'
        orders = []
        for s, q in pending['target'].items():
            delta = q - positions.get(s, 0)
            if (delta < -1e-8 and selling) or (delta > 1e-8 and not selling):
                orders.append((s, abs(delta)))
        prices = {s: float(daily[date, s][field]) for s, _ in orders}
        cost = sum(q * prices[s] for s, q in orders)
        ratio = 1 if selling or not cost else min(1, state['cash'] / cost)
        for s, q in orders:
            q *= ratio
            if q <= 1e-8:
                continue
            sign = -1 if selling else 1
            positions[s] += sign * q
            state['cash'] -= sign * q * prices[s]
            pending['done'][s] += q
            fills.append(dict(date=str(date.date()), phase=field, symbol=s, direction=side,
                              quantity=q, price=prices[s], fee=0.))
        assert state['cash'] >= -1e-6

    def sell_phase(date, prev):
        if policy.sizing == 'retarget' and date != pending['sell']:
            pending['target'] = lot_target(nav_at(prev), pending['weights'], marks(prev), policy)
        anchors = pending['sell_anchor'] if policy.anchor == 'fixed' else marks(prev)
        band = policy.sell_band(pending['sell_attempts'])
        pending['sell_attempts'] += 1
        for s in sorted(pending['target']):
            excess = positions.get(s, 0) - pending['target'][s]
            if excess <= 0:
                continue
            attempt(s, 'SELL')
            bar = daily.get((date, s))
            price, why = auction_fill(bar, -1, sell_limit(anchors[s], band), slip_bps, False)
            if price is None:
                note(date, s, 'SELL', why, excess)
                continue
            quantity = min(excess, int(float(bar['volume']) * 100 * participation)) // LOT * LOT
            if quantity > 0:
                book(date, s, 'SELL', quantity, price, 'close')
            if quantity < excess:
                note(date, s, 'SELL', 'CAPACITY', excess - quantity)

    def buy_phase(date, prev):
        known = marks(prev)
        anchors = pending['buy_anchor'] if policy.anchor == 'fixed' else known
        residual = {s: q - positions.get(s, 0) for s, q in pending['target'].items() if q > positions.get(s, 0)}
        for s in residual:
            attempt(s, 'BUY')
        if policy.sizing == 'replan':
            fresh = lot_target(nav_at(prev), pending['weights'], known, policy)
            for s in list(residual):
                kept = min(residual[s], max(0, fresh[s] - positions.get(s, 0)))
                if kept < residual[s]:
                    note(date, s, 'BUY', 'REPLAN_CAP', residual[s] - kept)
                residual[s] = kept
        orders = [dict(symbol=s, direction='BUY', quantity=int(q), limit_price=buy_limit(anchors[s], policy.buy_band(s)))
                  for s, q in sorted(residual.items()) if q > 0]
        if not orders:
            return
        allocated = {o['symbol']: o['quantity'] for o in allocate_buys(orders, max(0., state['cash']), LOT, {})}
        for order in orders:
            s = order['symbol']
            quantity = allocated.get(s, 0)
            if quantity < order['quantity']:
                note(date, s, 'BUY', 'CASH', order['quantity'] - quantity)
            if not quantity:
                continue
            bar = daily.get((date, s))
            price, why = auction_fill(bar, 1, order['limit_price'], slip_bps, policy.touch)
            if price is None:
                note(date, s, 'BUY', why, quantity)
                continue
            capped = min(quantity, int(float(bar['volume']) * 100 * participation)) // LOT * LOT
            why = 'CAPACITY' if capped < quantity else None
            while capped > 0 and capped * price + fee(capped * price) > state['cash'] + 1e-6:
                capped -= LOT
                why = why or 'CASH'
            if capped > 0:
                book(date, s, 'BUY', capped, price, 'open')
            if capped < quantity:
                note(date, s, 'BUY', why, quantity - capped)

    for idx, date in enumerate(dates):
        for a in ex[date]:
            s = a['symbol']
            factor = a['factor']
            if factor != 1:
                positions[s] = positions.get(s, 0) * factor if ideal else round(positions.get(s, 0) * factor)
                if pending:
                    for book_name in ('target', 'frozen'):
                        pending[book_name][s] = pending[book_name].get(s, 0) * factor
                    pending['sell_anchor'][s] /= factor
                    pending['buy_anchor'][s] /= factor
                    pending['done'][s] *= factor
                    for o in pending['original']:
                        if o['symbol'] == s:
                            o['quantity'] *= factor
                            o['reference_price'] /= factor
            if a['cash']:
                receivables.append([a['pay_date'], rights.get((s, a['ex_date']), 0) * a['cash']])
                if pending:
                    pending['sell_anchor'][s] -= a['cash']
                    pending['buy_anchor'][s] -= a['cash']
        for r in receivables:
            if r[0] <= date and r[1]:
                state['cash'] += r[1]
                r[1] = 0.
        if pending:
            first_buy = pending['buy']
            age = idx - dates.get_loc(first_buy) + 1
            prev = dates[idx - 1] if idx else None
            if 1 <= age <= window and prev is not None and (date - prev).days == 1 and age > terminal_delay:
                if ideal:
                    ideal_phase(date, False)
                else:
                    buy_phase(date, prev)
            next_date = dates[idx + 1] if idx + 1 < len(dates) else None
            eligible_sell = (date >= pending['sell'] and age < window and next_date is not None
                             and (next_date - date).days == 1 and date >= first_buy - pd.Timedelta(days=1))
            if eligible_sell and (age <= 0 or age > terminal_delay):
                if ideal:
                    ideal_phase(date, True)
                else:
                    sell_phase(date, prev)
            if date == pending['sell']:
                # Official reference close known after the sale; fixed anchor for
                # every opening buy attempt of this cycle unless rolling.
                pending['buy_anchor'] = marks(date)
            if age >= window:
                finish(date, 'WINDOW_EXPIRED')
                pending = None
        if date in weights.index:
            if pending:
                finish(date, 'SUPERSEDED')
            target_weights = {s: float(w) for s, w in weights.loc[date].items()}
            sell, buy = next_pair(dates, date)
            if arm != 'signal_ideal' and (buy is None or dates.get_loc(buy) + window - 1 >= len(dates)):
                raise ValueError('Insufficient common execution horizon')
            prices = marks(date)
            nav = nav_at(date)
            if ideal:
                target = {s: nav * w / prices[s] if w > 0 else 0 for s, w in target_weights.items()}
                gap = 0.
            else:
                target = lot_target(nav, target_weights, prices, policy)
                gap = sum(max(0, (1 - policy.reserve) * w - target[s] * prices.get(s, 0) / nav)
                          for s, w in target_weights.items())
            original = []
            for s in sorted(target):
                delta = target[s] - positions.get(s, 0)
                if abs(delta) > 1e-8:
                    original.append(dict(symbol=s, direction='BUY' if delta > 0 else 'SELL',
                                         quantity=abs(delta), reference_price=prices[s]))
            pending = dict(signal=date, sell=sell, buy=buy, target=dict(target), frozen=dict(target),
                           weights=target_weights, sell_anchor=prices.copy(), buy_anchor=prices.copy(),
                           done=defaultdict(float), reason={}, original=original, lot_gap=gap,
                           sell_attempts=0)
            if arm == 'signal_ideal':
                for side in ('SELL', 'BUY'):
                    side_orders = [o for o in original if o['direction'] == side]
                    cost = sum(o['quantity'] * prices[o['symbol']] for o in side_orders)
                    ratio = 1 if side == 'SELL' or not cost else min(1, max(0, state['cash']) / cost)
                    for o in side_orders:
                        sign = 1 if side == 'BUY' else -1
                        quantity = o['quantity'] * ratio
                        if quantity <= 1e-8:
                            continue
                        state['cash'] -= sign * quantity * prices[o['symbol']]
                        positions[o['symbol']] += sign * quantity
                        pending['done'][o['symbol']] += quantity
                assert state['cash'] >= -1e-5
                finish(date, 'IDEAL_COMPLETE')
                pending = None
        for a in records[date]:
            rights[a['symbol'], a['ex_date']] = positions.get(a['symbol'], 0)
        nav = nav_at(date)
        assert math.isfinite(nav) and nav > 0 and state['cash'] >= -1e-5 and min(positions.values()) >= -1e-5
        history.append(dict(date=date, nav=nav, cash=state['cash'],
                            receivables=sum(r[1] for r in receivables), fees=state['fees']))
    assert pending is None
    hist = pd.DataFrame(history).set_index('date')
    years = (hist.index[-1] - hist.index[0]).days / 365.25
    gross = hist.nav.iloc[-1] / capital
    curve = pd.Series([capital, *hist.nav])
    frame = pd.DataFrame(cycles).fillna(0.)
    return dict(
        arm=arm if ideal else policy.name, capital=capital, slip_bps=slip_bps, participation=participation,
        terminal_delay=terminal_delay, start=str(dates[0].date()), end=str(dates[-1].date()),
        cycles=len(frame), total_return=float(gross - 1), cagr=float(gross ** (1 / years) - 1),
        max_drawdown=float((curve / curve.cummax() - 1).min()), fees=state['fees'],
        mean_cash_weight=float((hist.cash / hist.nav).mean()),
    ), hist, frame, pd.DataFrame(fills), pd.DataFrame(events)


def cycle_stats(frame, skip_first):
    """Summaries over cycles; ``skip_first`` drops the initial cash build."""
    c = frame.iloc[1:] if skip_first else frame
    cause_cols = sorted(col for col in c.columns if col.startswith('cause_'))
    intended = c.buy_intended.sum()
    return dict(
        lot_gap_mean=float(c.lot_gap.mean()), lot_gap_max=float(c.lot_gap.max()),
        exec_uw_mean=float(c.exec_underweight.mean()), exec_uw_median=float(c.exec_underweight.median()),
        exec_uw_p95=float(np.quantile(c.exec_underweight, .95)), exec_uw_max=float(c.exec_underweight.max()),
        exec_uw_cycles_above_2pct=int((c.exec_underweight > .02).sum()),
        exec_ow_mean=float(c.exec_overweight.mean()), exec_ow_max=float(c.exec_overweight.max()),
        total_uw_mean=float(c.underweight.mean()), total_uw_max=float(c.underweight.max()),
        total_uw_cycles_above_2pct=int((c.underweight > .02).sum()),
        buy_completion=float(c.buy_filled.sum() / intended) if intended else None,
        cause_mean={col[6:]: float(c[col].mean()) for col in cause_cols},
        cycles=len(c),
    )
