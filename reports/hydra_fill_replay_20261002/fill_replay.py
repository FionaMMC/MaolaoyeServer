"""Offline bar replay of frozen whole-order cash checks and next-day replans.

No network, order gateway or production database. A bar is NOT a 30-second
broker report. Sell fills become observable/spendable only on a later bar.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math

from allocation_code import allocation_policy, replan_residual
from legacy_code import guarded_orders
from queue_code import cash_readiness, submission_order, buy_reservation_cents


def fee(gross):
    return max(5., gross * .0001) if gross else 0.


def fixed_orders(target, positions, anchors):
    result = []
    for symbol in sorted(target):
        delta = target[symbol] - positions.get(symbol, 0)
        if not delta:
            continue
        anchor = Decimal(str(anchors[symbol]))
        raw = anchor * (Decimal('1.005') if delta > 0 else Decimal('.995'))
        limit = (raw * 1000).to_integral_value(rounding=ROUND_FLOOR if delta > 0 else ROUND_CEILING) / 1000
        result.append(dict(symbol=symbol, direction='BUY' if delta > 0 else 'SELL',
                           quantity=abs(delta), reference_price=float(anchor), limit_price=float(limit)))
    return result


def session(state, orders, bars, *, lag_bars=1, participation=.01, slip_bps=5., touch=True):
    """All fills chronological; submitted whole buys reserve cash until close."""
    assert lag_bars >= 1  # never use an ambiguous within-bar sale to fund a buy
    initial_cash = state['cash']
    rows = []
    for index, order in enumerate(orders):
        rows.append({**order, 'order_id': str(index), 'submit_status': 'PREPARED',
                     'execution_policy': {'cash_allocation': allocation_policy({})},
                     'filled': 0, 'gross': 0., 'fee': 0., 'submitted_at': None})
    rows = submission_order(rows)
    reported = {}
    reports = []
    fills = []
    minimum_cash = state['cash']
    deferred_checks = 0
    for index, time in enumerate(sorted(bars)):
        outstanding = []
        for report in reports:
            if report['due'] <= index:
                reported[report['order_id']] = report['fill']
            else:
                outstanding.append(report)
        reports = outstanding
        for row in rows:
            if row['submit_status'] == 'SUBMITTED':
                continue
            if row['direction'] == 'BUY':
                # Model QMT's remaining order freezes, distinct from the client's
                # deliberately conservative full-submission reservation.
                locked = sum(max(0., buy_reservation_cents(r) / 100 - r['gross'] - r['fee'])
                             for r in rows if r['direction'] == 'BUY'
                             and r['submit_status'] == 'SUBMITTED' and r['filled'] < r['quantity'])
                unavailable_proceeds = sum(r['net_cash'] for r in reports)
                available = max(0., state['cash'] - locked - unavailable_proceeds)
                readiness = cash_readiness(row, initial_owned_cash=initial_cash,
                                           qmt_available_cash=available,
                                           submissions=rows, confirmed_sell_fills=reported)
                if not readiness['ready']:
                    deferred_checks += 1
                    continue
            row['submit_status'] = 'SUBMITTED'
            row['submitted_at'] = str(time)
        for row in rows:
            if row['submit_status'] != 'SUBMITTED' or row['filled'] == row['quantity']:
                continue
            bar = bars[time].get(row['symbol'])
            if bar is None or bar['volume'] <= 0 or bar['high'] <= bar['low'] or bar.get('suspendFlag', 0):
                continue
            side = 1 if row['direction'] == 'BUY' else -1
            limit = row['limit_price']
            arrival = float(bar['open']) * (1 + side * slip_bps / 10000)
            price = (math.ceil(arrival * 1000 - 1e-8) if side > 0 else math.floor(arrival * 1000 + 1e-8)) / 1000
            if side * (price - limit) > 1e-8:
                crossed = bar['low'] < limit - .0005 if side > 0 else bar['high'] > limit + .0005
                if not touch or not crossed:
                    continue
                price = limit
            capacity = int(bar['volume'] * participation) // 100 * 100
            quantity = min(row['quantity'] - row['filled'], capacity) // 100 * 100
            if not quantity:
                continue
            extra_fee = fee(row['gross'] + quantity * price) - row['fee']
            state['cash'] -= side * quantity * price + extra_fee
            state['positions'][row['symbol']] = state['positions'].get(row['symbol'], 0) + side * quantity
            assert state['cash'] >= -1e-6 and state['positions'][row['symbol']] >= 0
            minimum_cash = min(minimum_cash, state['cash'])
            row['filled'] += quantity
            row['gross'] += quantity * price
            row['fee'] += extra_fee
            if side < 0:
                reports.append(dict(due=index + lag_bars, order_id=row['order_id'],
                                    net_cash=quantity * price - extra_fee,
                                    fill={'filled_quantity': row['filled'],
                                          'filled_price': row['gross'] / row['filled']}))
            fills.append(dict(time=str(time), symbol=row['symbol'], direction=row['direction'],
                              quantity=quantity, price=price, fee=extra_fee))
    # Assumption: end-of-day broker reconciliation proves all active residuals
    # cancelled before another session. Unknown-finality scenarios use episode's
    # explicit gate and cannot simply reuse this assumption.
    return dict(rows=rows, fills=fills, minimum_cash=minimum_cash, cash_wait_checks=deferred_checks)


def episode(snapshot, dates, close, daily, intraday, *, arm, lag_bars=1,
            participation=.01, slip_bps=5., touch=True, broker_final=True, cash_buffer=0.0):
    state = {'cash':float(snapshot['cash']), 'positions':dict(snapshot['positions'])}
    original = fixed_orders(snapshot['target'], state['positions'], snapshot['anchors'])
    details = {o['symbol']: dict(symbol=o['symbol'], direction=o['direction'],
                 requested_quantity=o['quantity'], filled_quantity=0, day1_quantity=0,
                 anchor=o['reference_price'], last_reason='WINDOW_EXPIRED') for o in original}
    day_records, fill_records = [], []
    minimum_cash = state['cash']
    ever_submitted = set()
    for age, date in enumerate(dates, 1):
        previous = close.index[close.index.get_loc(date) - 1]
        if arm == 'one_day' and age > 1:
            continue
        if age > 1 and not broker_final:
            day_records.append(dict(date=str(date.date()), reason='PREVIOUS_BROKER_NOT_FINAL'))
            continue
        if (date - previous).days != 1:
            day_records.append(dict(date=str(date.date()), reason='NON_ADJACENT_TRADING_DAY'))
            continue
        audit = {}
        if age == 1:
            orders = deepcopy(original)
        elif arm == 'fixed_quantity_3d':
            orders = guarded_orders(fixed_orders(snapshot['target'], state['positions'], snapshot['anchors']),
                                    snapshot['anchors'], state['cash'], 100)
        else:
            prices = {s:float(p) for s,p in close.loc[previous].items() if math.isfinite(p) and p > 0}
            blocked = {s for s in snapshot['target'] if (previous,s) not in daily
                       or daily[previous,s].get('suspendFlag',0) or daily[previous,s]['volume'] <= 0}
            orders,audit = replan_residual(target_shares=snapshot['target'], weights=snapshot['weights'],
                    cash_buffer_weight=cash_buffer, actual_cash=state['cash'],
                    actual_positions={s:q for s,q in state['positions'].items() if q},
                    prices=prices, anchors=snapshot['anchors'], lot_size=100,
                    priorities={}, blocked_symbols=blocked)
            for symbol,reason in audit['deferred_symbols'].items():
                details[symbol]['last_reason'] = reason
        result = session(state, orders, intraday[date], lag_bars=lag_bars,
                         participation=participation, slip_bps=slip_bps, touch=touch)
        minimum_cash = min(minimum_cash, result['minimum_cash'])
        for row in result['rows']:
            detail = details[row['symbol']]
            if row['submit_status'] == 'SUBMITTED':
                ever_submitted.add(row['symbol'])
            if row['filled'] < row['quantity']:
                detail['last_reason'] = ('CASH_NOT_READY' if row['submit_status'] != 'SUBMITTED'
                                         else 'PRICE_OR_CAPACITY')
        for fill in result['fills']:
            detail = details[fill['symbol']]
            assert detail['direction'] == fill['direction']
            detail['filled_quantity'] += fill['quantity']
            if age == 1:
                detail['day1_quantity'] += fill['quantity']
            assert detail['filled_quantity'] <= detail['requested_quantity']
            fill_records.append(dict(day=age, **fill))
        day_records.append(dict(date=str(date.date()), age=age, cash_end=state['cash'],
                     planned_orders=len(orders), submitted_orders=sum(r['submit_status']=='SUBMITTED' for r in result['rows']),
                     cash_wait_checks=result['cash_wait_checks'], audit=audit,
                     planned_reference_notional=sum(o['quantity']*snapshot['anchors'][o['symbol']] for o in orders)))
    for detail in details.values():
        detail['ever_submitted'] = detail['symbol'] in ever_submitted
        detail['remaining_quantity'] = detail['requested_quantity'] - detail['filled_quantity']
        detail['filled_reference_notional'] = detail['filled_quantity'] * detail['anchor']
        detail['original_reference_notional'] = detail['requested_quantity'] * detail['anchor']
        detail['day1_reference_notional'] = detail['day1_quantity'] * detail['anchor']
        if not detail['remaining_quantity']:
            detail['last_reason'] = 'COMPLETE'
    marks = close.loc[dates[-1]]
    nav = state['cash'] + sum(q*float(marks[s]) for s,q in state['positions'].items() if q)
    distance = .5 * (abs(state['cash']/nav-cash_buffer) + sum(
        abs(state['positions'].get(s,0)*float(marks[s])/nav-(1-cash_buffer)*w) if math.isfinite(float(marks[s])) else (1-cash_buffer)*w
        for s,w in snapshot['weights'].items()))
    return dict(details=list(details.values()), days=day_records, fills=fill_records,
                starting_cash=snapshot['cash'], starting_positions=dict(snapshot['positions']),
                minimum_cash=minimum_cash, ending_cash=state['cash'], allocation_distance=distance,
                positions=state['positions'])
