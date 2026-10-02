"""Continuous, offline close-sell/open-buy scenario model. Never an order router.

Signal known after dated close. First sale strictly after that date. Orders are
frozen from known prices. Official close is an execution proxy, not a guaranteed
auction fill. All quantities/cash carry across months; no monthly resets.
"""
from collections import defaultdict
import math

import numpy as np
import pandas as pd

from allocation_code import allocate_buys, replan_residual
from fill_replay import fixed_orders, session


def next_pair(dates, signal):
    return next(((a,b) for a,b in zip(dates[:-1],dates[1:]) if a>signal and (b-a).days==1), (None,None))


def invest_target(nav, weights, prices, *, ideal=False):
    return {s:(nav*float(w)/prices[s] if ideal else math.floor(nav*float(w)/prices[s]/1.006005/100)*100)
            if w>0 else 0 for s,w in weights.items()}


def run(weights, daily, close, actions, *, arm='replan', capital=200000.,
        slip_bps=5., participation=.01, terminal_delay=0, cash_buffer=0., reserve_spendable=True):
    if arm not in ('signal_ideal','pair_ideal','fixed','replan'):
        raise ValueError(arm)
    ideal=arm.endswith('ideal')
    dates=close.loc[weights.index.min():].index
    positions=dict.fromkeys(weights.columns,0)
    state={'positions':positions,'cash':float(capital)}
    rights,receivables={},[]
    ex,records=defaultdict(list),defaultdict(list)
    for a in actions:
        ex[a['ex_date']].append(a)
        if a['record_date'] is not None:
            records[a['record_date']].append(a)
    pending=None
    history,cycles,fills,events=[],[],[],[]
    target_weights=None
    fees=0.

    def marks(date):
        return {s:float(v) for s,v in close.loc[date].items() if math.isfinite(v) and v>0}

    def nav_at(date):
        prices=marks(date)
        return state['cash']+sum(q*prices[s] for s,q in positions.items() if q)+sum(r[1] for r in receivables)

    def finish(date, reason):
        if pending is None:
            return
        prices=marks(date)
        nav=nav_at(date)
        under=sum(max(0,(1-cash_buffer)*float(w)-positions.get(s,0)*prices.get(s,0)/nav) for s,w in pending['weights'].items())
        distance=.5*(sum(abs((1-cash_buffer)*float(w)-positions.get(s,0)*prices.get(s,0)/nav) for s,w in pending['weights'].items())+abs((state['cash']+sum(r[1] for r in receivables))/nav-cash_buffer))
        requested=pending['original']
        done=pending['done']
        cycles.append(dict(signal=str(pending['signal'].date()),end=str(date.date()),reason=reason,
            underweight=under,allocation_distance=distance,
            uninvested_vs_full_target=sum(max(0,float(w)-positions.get(s,0)*prices.get(s,0)/nav) for s,w in pending['weights'].items()),
            buy_intended=sum(x['quantity']*x['reference_price'] for x in requested if x['direction']=='BUY'),
            buy_filled=sum(min(done.get(x['symbol'],0),x['quantity'])*x['reference_price'] for x in requested if x['direction']=='BUY'),
            sell_intended=sum(x['quantity']*x['reference_price'] for x in requested if x['direction']=='SELL'),
            sell_filled=sum(min(done.get(x['symbol'],0),x['quantity'])*x['reference_price'] for x in requested if x['direction']=='SELL')))

    def execute(date, orders, selling):
        nonlocal fees
        if not orders:
            return
        if ideal:
            # No fee/lot/volume/limit constraint; still no borrowing or spending
            # dividend receivables. Buy scale uses prices at this event only.
            field='close' if selling else 'open'
            prices={o['symbol']:float(daily[date,o['symbol']][field]) for o in orders}
            cost=sum(o['quantity']*prices[o['symbol']] for o in orders)
            ratio=1 if selling or not cost else min(1,state['cash']/cost)
            for o in orders:
                q=o['quantity']*ratio
                if q<=1e-8:
                    continue
                side=-1 if selling else 1
                positions[o['symbol']]+=side*q
                state['cash']-=side*q*prices[o['symbol']]
                pending['done'][o['symbol']]+=q
                fills.append(dict(date=str(date.date()),phase=field,symbol=o['symbol'],direction=o['direction'],quantity=q,price=prices[o['symbol']],fee=0.))
            assert state['cash']>=-1e-6
            return
        # One synthetic event, no intraday recovery assumed. A whole buy must
        # already fit actual cash before the source queue will submit it.
        event_bars={}
        for o in orders:
            bar=daily.get((date,o['symbol']))
            if bar is None:
                continue
            price=float(bar['close' if selling else 'open'])
            event_bars[o['symbol']]={**bar,'open':price,'volume':float(bar['volume'])*100}
        result=session(state,orders,{0:event_bars},slip_bps=slip_bps,
                       participation=participation,touch=False)
        for f in result['fills']:
            pending['done'][f['symbol']]+=f['quantity']
            fees+=f['fee']
            fills.append(dict(date=str(date.date()),phase='close' if selling else 'open',**f))
        for row in result['rows']:
            if row['filled']<row['quantity']:
                events.append(dict(date=str(date.date()),symbol=row['symbol'],reason='CASH_OR_OPEN_PRICE_OR_CAPACITY',unfilled=row['quantity']-row['filled']))

    for idx,date in enumerate(dates):
        for a in ex[date]:
            s=a['symbol']
            factor=a['factor']
            if factor!=1:
                positions[s]=positions.get(s,0)*factor if ideal else round(positions.get(s,0)*factor)
                if pending:
                    pending['target'][s]=pending['target'].get(s,0)*factor
                    pending['anchors'][s]/=factor
                    pending['buy_anchors'][s]/=factor
                    pending['done'][s]*=factor
                    for o in pending['original']:
                        if o['symbol']==s:
                            o['quantity']*=factor
                            o['reference_price']/=factor
            if a['cash']:
                receivables.append([a['pay_date'],rights.get((s,a['ex_date']),0)*a['cash']])
                if pending:
                    pending['anchors'][s]-=a['cash']
                    pending['buy_anchors'][s]-=a['cash']
        for r in receivables:
            if r[0]<=date and r[1]:
                state['cash']+=r[1]
                r[1]=0.
        if pending:
            first_buy=pending['buy']
            age=idx-dates.get_loc(first_buy)+1
            prev=dates[idx-1] if idx else None
            eligible_buy=1<=age<=3 and prev is not None and (date-prev).days==1
            if eligible_buy and age>terminal_delay:
                anchors=pending['buy_anchors']
                if arm=='replan':
                    known=marks(prev)
                    blocked={s for s in pending['target'] if (prev,s) not in daily or daily[prev,s].get('suspendFlag',0) or daily[prev,s]['volume']<=0}
                    orders,audit=replan_residual(target_shares=pending['target'],weights={s:w*(1-cash_buffer) for s,w in pending['weights'].items()} if reserve_spendable else pending['weights'],cash_buffer_weight=0. if reserve_spendable else cash_buffer,
                        actual_positions={s:q for s,q in positions.items() if q},actual_cash=state['cash'],
                        prices=known,anchors=anchors,lot_size=100,priorities={},blocked_symbols=blocked)
                    for s,reason in audit['deferred_symbols'].items():
                        events.append(dict(date=str(date.date()),symbol=s,reason=reason))
                else:
                    orders=fixed_orders(pending['target'],positions,anchors)
                    if not ideal:
                        # Fixed residual ceiling; split across symbols before
                        # submission when total cash is insufficient.
                        budget=max(0.,state['cash']-(0. if reserve_spendable else cash_buffer*nav_at(prev)))
                        orders=allocate_buys([o for o in orders if o['direction']=='BUY'],budget,100,{})
                execute(date,[o for o in orders if o['direction']=='BUY'],False)
            # Only a close whose following natural day is an eligible buy date
            # may sell. Friday/Monday gap never creates weekend exposure by sale.
            next_date=dates[idx+1] if idx+1<len(dates) else None
            eligible_sell=(date>=pending['sell'] and age<3 and next_date is not None
                           and (next_date-date).days==1 and date>=first_buy-pd.Timedelta(days=1))
            if eligible_sell and (age<=0 or age>terminal_delay):
                orders=fixed_orders(pending['target'],positions,pending['anchors'])
                execute(date,[o for o in orders if o['direction']=='SELL'],True)
            if date==pending['sell']:
                # Official reference close is now known, after the sale phase;
                # fixed for all opening buy attempts, never rolled upward.
                pending['buy_anchors']=marks(date)
            if age>=3:
                finish(date,'WINDOW_EXPIRED')
                pending=None
        if date in weights.index:
            if pending:
                finish(date,'SUPERSEDED')
            target_weights={s:float(w) for s,w in weights.loc[date].items()}
            sell,buy=next_pair(dates,date)
            if arm!='signal_ideal' and (buy is None or dates.get_loc(buy)+2>=len(dates)):
                raise ValueError('Insufficient common execution horizon')
            prices=marks(date)
            target=invest_target(nav_at(date)*(1-cash_buffer),target_weights,prices,ideal=ideal)
            pending=dict(signal=date,sell=sell,buy=buy,target=target,weights=target_weights,
                         anchors=prices.copy(),buy_anchors=prices.copy(),done=defaultdict(float),
                         original=fixed_orders(target,positions,prices))
            if arm=='signal_ideal':
                # Pure frictionless signal-close benchmark, explicitly not a
                # claim the signal can be observed before the same close.
                for side in ('SELL','BUY'):
                    side_orders=[o for o in pending['original'] if o['direction']==side]
                    cost=sum(o['quantity']*prices[o['symbol']] for o in side_orders)
                    ratio=1 if side=='SELL' or not cost else min(1,max(0,state['cash'])/cost)
                    for o in side_orders:
                        sign=1 if side=='BUY' else -1
                        quantity=o['quantity']*ratio
                        if quantity<=1e-8:
                            continue
                        state['cash']-=sign*quantity*prices[o['symbol']]
                        positions[o['symbol']]+=sign*quantity
                        pending['done'][o['symbol']]+=quantity
                        fills.append(dict(date=str(date.date()),phase='close',symbol=o['symbol'],direction=side,quantity=quantity,price=prices[o['symbol']],fee=0.))
                assert state['cash']>=-1e-5
                finish(date,'IDEAL_COMPLETE')
                pending=None
        for a in records[date]:
            rights[a['symbol'],a['ex_date']]=positions.get(a['symbol'],0)
        nav=nav_at(date)
        assert math.isfinite(nav) and nav>0 and state['cash']>=-1e-5 and min(positions.values())>=-1e-5
        history.append(dict(date=date,nav=nav,cash=state['cash'],receivables=sum(r[1] for r in receivables),fees=fees))
    assert pending is None
    hist=pd.DataFrame(history).set_index('date')
    years=(hist.index[-1]-hist.index[0]).days/365.25
    gross=hist.nav.iloc[-1]/capital
    augmented=pd.Series([capital,*hist.nav])
    original_buy=sum(c['buy_intended'] for c in cycles)
    summary=dict(arm=arm,capital=capital,slip_bps=slip_bps,participation=participation,terminal_delay=terminal_delay,reserve_spendable=reserve_spendable,
                 start=str(dates[0].date()),end=str(dates[-1].date()),cycles=len(cycles),cash_buffer_weight=cash_buffer,
                 total_return=float(gross-1),cagr=float(gross**(1/years)-1),max_drawdown=float((augmented/augmented.cummax()-1).min()),
                 minimum_cash=float(hist.cash.min()),fees=fees,buy_completion=sum(c['buy_filled'] for c in cycles)/original_buy if original_buy else None,
                 mean_end_underweight=float(np.mean([c['underweight'] for c in cycles])),
                 max_end_underweight=max(c['underweight'] for c in cycles),cycles_above_2pct=sum(c['underweight']>.02 for c in cycles),
                 p95_end_underweight=float(np.quantile([c['underweight'] for c in cycles],.95)),
                 reason_counts={k:int(v) for k,v in pd.Series([e['reason'] for e in events],dtype=str).value_counts().items()})
    return summary,hist,pd.DataFrame(cycles),pd.DataFrame(fills),pd.DataFrame(events)
