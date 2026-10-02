import sys
from pathlib import Path
import unittest
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'execution_experiment_20260928'))
from final_suite import Intraday, Policy, FocusWindow, load_frequency
from test_server_intraday import setup, GOLD


class ExtendedWindows(unittest.TestCase):
    def make(self):
        e,state,pending,first=setup({GOLD:[['09:35',10.,10.1,9.9,10.,1000000]]})
        e.__class__=Intraday
        e.adjacent=False
        frames=[]
        for i,date in enumerate(e.close.index[1:6],1):
            # Only day four becomes accessible below the fixed limit.
            price=10. if i==4 else 12.
            frames.append(dict(time=date+pd.Timedelta(hours=9,minutes=35),date=date,
                open=price,high=price+.1,low=price-.01,close=price,volume=1000000))
        e.prepare({GOLD:pd.DataFrame(frames)})
        return e,state,pending

    def test_three_days_cannot_fill_day_four_but_five_can(self):
        e,state,pending=self.make()
        short,_=e.episode((state,pending),Policy('three',sessions=3))
        long,_=e.episode((state,pending),Policy('five',sessions=5))
        self.assertEqual(short[0]['filled'],0)
        self.assertGreater(long[0]['filled'],0)
        self.assertEqual(short[0]['intended'],long[0]['intended'])

    def test_two_days_does_not_silently_extend_to_five(self):
        e,state,pending=self.make()
        rows,_=e.episode((state,pending),Policy('two',sessions=2))
        self.assertEqual(rows[0]['filled'],0)

    def test_snapshot_is_never_mutated(self):
        e,state,pending=self.make()
        start_cash=state['cash'];qty=state['qty'].copy()
        e.episode((state,pending),Policy('five',sessions=5))
        self.assertEqual(state['cash'],start_cash)
        self.assertEqual(state['qty'],qty)
        self.assertEqual(pending['age'],0)

    def test_focus_extension_lets_gold_buy_on_day_four(self):
        e,state,pending=self.make()
        rows,_=e.episode((state,pending),FocusWindow('focusfive',sessions=5,focus_sessions=5))
        self.assertGreater(rows[0]['filled'],0)

    def test_focus_one_day_does_not_keep_gold_live_on_day_four(self):
        e,state,pending=self.make()
        rows,_=e.episode((state,pending),FocusWindow('focusone',sessions=3,focus_sessions=1))
        self.assertEqual(rows[0]['filled'],0)

    def test_focus_extension_does_not_extend_sell_window(self):
        e,state,pending=self.make()
        state['qty'][GOLD]=600
        # A sell below 9.95 cannot execute until the fourth day at 10.
        for (date,symbol),frame in e.intraday.items():
            if date!=e.close.index[4]:
                for col in ['open','high','low','close']:frame[col]-=4.
        rows,_=e.episode((state,pending),FocusWindow('focusfive',sessions=5,focus_sessions=5))
        self.assertEqual(rows[0]['filled'],0)


if __name__=='__main__':unittest.main()
