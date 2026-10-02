"""Validate public minute bars; bounded intraday opportunity study, not live fills."""
from pathlib import Path
import json
import math
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
SYMBOLS = ['518880.SH','513100.SH','513500.SH']


def load():
    frames = {}
    evidence = []
    raw = pd.read_parquet(HERE.parent/'execution_experiment_20260927/private/hydra_raw.parquet')
    raw['date'] = pd.to_datetime(raw.trade_date.astype(str))
    for symbol in SYMBOLS:
        code = symbol[:6]
        d = pd.DataFrame(json.loads((HERE/'public_minutes'/f'sina_{code}_5_20500101.json').read_text()))
        d['time'] = pd.to_datetime(d.day)
        for col in ['open','high','low','close','volume','amount']:
            d[col] = pd.to_numeric(d[col])
        assert not d.time.duplicated().any()
        assert ((d.low<=d[['open','close']].min(axis=1))&(d.high>=d[['open','close']].max(axis=1))).all()
        d['date'] = d.time.dt.normalize()
        full = d[d.date>d.date.min()].copy()  # drop truncated first day
        daily = full.groupby('date').agg(open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum'),bars=('close','size'),first=('time','min'))
        q = raw[raw.symbol==symbol].set_index('date')
        joined = daily.join(q[['open','high','low','close','volume']],rsuffix='_qmt').dropna()
        # QMT raw daily volume is lots; Sina minute volume is shares.
        volume_ratio = joined.volume/(joined.volume_qmt*100)
        one = json.loads((HERE/'public_minutes'/f'eastmoney_1m_{code}_1_20500101.json').read_text())['data']['trends']
        m = pd.DataFrame([r.split(',') for r in one],columns=['time','open','close','high','low','volume','amount','average'])
        m['time'] = pd.to_datetime(m.time)
        for col in m.columns[1:]:m[col]=pd.to_numeric(m[col])
        # This endpoint's open field is zero: it is not a usable minute open.
        nonzero = m[m.volume>0].copy()
        em5 = nonzero.set_index('time').resample('5min',closed='right',label='right').agg(high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum')).dropna()
        both = d.set_index('time').join(em5,rsuffix='_em').dropna(subset=['close_em'])
        first_nonzero = nonzero.groupby(nonzero.time.dt.strftime('%Y-%m-%d')).time.min().dt.strftime('%H:%M').to_dict()
        evidence.append(dict(symbol=symbol,sina_rows=len(d),sina_first=str(d.time.min()),sina_last=str(d.time.max()),
            usable_days=len(daily),sina_daily_bar_counts={str(k):int(v) for k,v in daily.bars.value_counts().items()},
            late_first_bar_days=int((daily['first'].dt.strftime('%H:%M')>'09:35').sum()),
            qmt_overlap_days=len(joined),qmt_close_max_abs_error=float((joined.close-joined.close_qmt).abs().max()),
            qmt_ohlc_max_abs_error={c:float((joined[c]-joined[c+'_qmt']).abs().max()) for c in ['open','high','low','close']},
            sina_to_qmt_volume_ratio_median=float(volume_ratio.median()),eastmoney_1m_rows=len(m),
            eastmoney_zero_open_fraction=float((m.open==0).mean()),cross_provider_5m_bars=len(both),
            cross_provider_close_max_error=float((both.close-both.close_em).abs().max()),
            cross_provider_close_match_within_tick=float(((both.close-both.close_em).abs()<=.001+1e-9).mean()),
            eastmoney_first_positive_volume=first_nonzero))
        frames[symbol] = full
    return frames,evidence


def replay(days,ref,quantity,policy,symbol,cutoff='14:55'):
    remaining = quantity
    cash = 10000.
    fills = []
    original_ref = ref
    for age, day in enumerate(days,1):
        date = day.date.iloc[0]
        previous_date = day.attrs['previous_date']
        if (date-previous_date).days != 1:
            continue
        reference = day.attrs['previous_close'] if policy=='rolling50' else original_ref
        bp = (100 if symbol=='518880.SH' else 150) if policy=='differentiated' else 50
        limit = math.floor(reference*(1+bp/10000)/.001+1e-8)*.001
        daily_fee_paid = False
        # A 14:55-ending bar may overlap cancellation; require bar end <14:55.
        for r in day[day.time.dt.strftime('%H:%M')<cutoff].itertuples():
            if r.volume<=0 or remaining<=0:continue
            price = math.ceil(r.open*1.0005/.001-1e-8)*.001
            if price>limit+1e-9:
                if r.low<limit-.0005:price=limit
                else:continue
            cap = int(r.volume*.01)//100*100
            fee = 0. if daily_fee_paid else 5.
            affordable = max(0,int((cash-fee)/price))//100*100
            n = min(remaining,cap,affordable)
            if n<=0:continue
            cash -= n*price+fee
            daily_fee_paid = True
            assert cash >= -1e-6
            fills.append(dict(quantity=n,price=price,age=age,time=str(r.time)))
            remaining -= n
    return dict(filled=quantity-remaining,first_day_filled=sum(x['quantity'] for x in fills if x['age']==1),
                first_fill=fills[0]['time'] if fills else None,price_cost=sum(x['quantity']*(x['price']-original_ref) for x in fills))


def main():
    frames,evidence = load()
    common = sorted(set.intersection(*[set(d.date) for d in frames.values()]))
    scenarios = []
    cutoff_events = []
    for symbol,d in frames.items():
        groups = {date:x.copy() for date,x in d.groupby('date')}
        for i in range(1,len(common)-2):
            date = common[i]
            if (date-common[i-1]).days != 1:continue
            ref = float(groups[common[i-1]].close.iloc[-1])
            # Identical target quantity and cash buffer for every policy.
            qty = int((10000-5)/(ref*1.015))//100*100
            days = []
            for j in range(i,i+3):
                day=groups[common[j]].copy()
                day.attrs.update(previous_date=common[j-1],previous_close=float(groups[common[j-1]].close.iloc[-1]))
                days.append(day)
            for policy in ['fixed50','rolling50','differentiated']:
                r = replay(days,ref,qty,policy,symbol)
                full = replay(days,ref,qty,policy,symbol,cutoff='15:01')
                scenarios.append(dict(symbol=symbol,date=str(date.date()),policy=policy,intended=qty*ref,
                    filled=r['filled']*ref,first_day_filled=r['first_day_filled']*ref,price_cost=r['price_cost'],
                    additional_after_cutoff=(full['filled']-r['filled'])*ref,first_fill=r['first_fill']))
            limit=math.floor(ref*1.005/.001+1e-8)*.001
            positive=days[0][days[0].volume>0]
            touched=(positive.low<limit-.0005)
            before=(positive[positive.time.dt.strftime('%H:%M')<'14:55'].low<limit-.0005)
            cutoff_events.append(dict(symbol=symbol,date=str(date.date()),daily_touch=bool(touched.any()),pre_cancel_touch=bool(before.any())))
    frame=pd.DataFrame(scenarios)
    frame.to_csv(HERE/'minute_opportunities.csv',index=False)
    summary=[]
    for (symbol,policy),d in frame.groupby(['symbol','policy']):
        summary.append(dict(symbol=symbol,policy=policy,hypothetical_orders=len(d),completion=d.filled.sum()/d.intended.sum(),
            first_day_completion=d.first_day_filled.sum()/d.intended.sum(),additional_after_cutoff=d.additional_after_cutoff.sum()/d.intended.sum(),
            conditional_price_cost_bps=d.price_cost.sum()/d.filled.sum()*10000 if d.filled.sum() else None))
    out=dict(data_quality=evidence,common_first=str(common[0].date()),common_last=str(common[-1].date()),common_days=len(common),
             results=summary,cutoff_false_opportunities=int(sum(r['daily_touch'] and not r['pre_cancel_touch'] for r in cutoff_events)),
             limitations=['Recent hypothetical buys, not historical strategy buys or realized orders.',
             'No bid/ask queue, IOPV history or auction queue; bar touches are still optimistic.',
             '5m bar volume participates at at most 1%; price-through does not guarantee fills.',
             'Missing/zero-volume minutes never become assumed tradable quotes.',
             'New observed price range is short; it cannot validate seven-year performance.'])
    (HERE/'minute_validation.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
    print(json.dumps(out,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
