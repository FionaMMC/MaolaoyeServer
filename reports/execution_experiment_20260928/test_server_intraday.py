import unittest
import pandas as pd
from server_intraday import MinuteReplay
from compare_policies import Policy

GOLD='518880.SH'
DOMESTIC='510300.SH'


def setup(rows, cash=10000., gold=0, domestic=0, target_gold=300, target_domestic=0):
    dates=pd.bdate_range('2024-12-23',periods=8)
    close=pd.DataFrame({GOLD:[10.]*8,DOMESTIC:[10.]*8},index=dates)
    weights=pd.DataFrame({GOLD:[1.],DOMESTIC:[0.]},index=dates[:1])
    engine=MinuteReplay(weights,{},close,[],capital=10000.,slip=5.,adjacent=False)
    state=engine.initial_state();state.update(cash=cash,qty={GOLD:gold,DOMESTIC:domestic})
    pending=engine.make_pending(state,dates[0],dates[1],weights.iloc[0])
    pending['target']={GOLD:target_gold,DOMESTIC:target_domestic}
    pending['detail']={}
    for symbol,current,target in [(GOLD,gold,target_gold),(DOMESTIC,domestic,target_domestic)]:
        if current!=target:
            pending['detail'][symbol]=dict(intended=abs(target-current)*10.,filled=0.,fees=0.,price_cost=0.,delay_notional=0.,first_day_filled=0.)
    minutes={}
    for symbol,data in rows.items():
        frame=pd.DataFrame(data,columns=['clock','open','high','low','close','volume'])
        frame['time']=pd.to_datetime(str(dates[1].date())+' '+frame.clock)
        frame['date']=dates[1]
        minutes[symbol]=frame
    engine.prepare(minutes)
    return engine,state,pending,dates[1]


class IntradayChecks(unittest.TestCase):
    def test_sell_cash_waits_until_next_bar(self):
        rows={s:[['09:35',10.,10.1,9.9,10.,100000],['09:40',10.,10.1,9.9,10.,100000]] for s in [GOLD,DOMESTIC]}
        e,s,p,d=setup(rows,cash=0.,domestic=200,target_gold=100)
        e.execute(s,d,p,Policy('fixed'))
        buys=[x for x in e.execution_events if x['side']==1]
        self.assertEqual(len(buys),1)
        self.assertTrue(buys[0]['time'].endswith('09:40:00'))
        self.assertGreaterEqual(s['cash'],0.)

    def test_partial_fills_charge_one_daily_minimum(self):
        rows={GOLD:[[t,10.,10.1,9.9,10.,15000] for t in ['09:35','09:40','09:45']]}
        e,s,p,d=setup(rows)
        e.execute(s,d,p,Policy('fixed'))
        self.assertEqual(s['qty'][GOLD],300)
        self.assertAlmostEqual(sum(x['fee'] for x in e.execution_events),5.)
        self.assertTrue(all(x['quantity']==100 for x in e.execution_events))

    def test_zero_volume_and_cancelled_period_do_not_fill(self):
        rows={GOLD:[['09:35',10.,10.1,9.9,10.,0],['14:55',10.,10.1,9.9,10.,100000]]}
        e,s,p,d=setup(rows)
        e.execute(s,d,p,Policy('fixed'))
        self.assertEqual(s['qty'][GOLD],0)
        self.assertEqual(e.execution_events,[])

    def test_cash_constraint_includes_fee_and_round_lots(self):
        rows={GOLD:[['09:35',10.,10.1,9.9,10.,1000000]]}
        e,s,p,d=setup(rows,target_gold=1000)
        e.execute(s,d,p,Policy('fixed'))
        self.assertEqual(s['qty'][GOLD],900)
        self.assertGreaterEqual(s['cash'],0.)


if __name__=='__main__':unittest.main()
