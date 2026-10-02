import pandas as pd

from validate_minutes import replay


def day(rows, date='2026-09-02', previous_close=10.):
    frame=pd.DataFrame(rows,columns=['clock','open','high','low','close','volume'])
    frame['time']=pd.to_datetime(date+' '+frame.clock)
    frame['date']=pd.Timestamp(date)
    frame.attrs.update(previous_date=pd.Timestamp(date)-pd.Timedelta(days=1),previous_close=previous_close)
    return frame


def test_cancellation_cutoff_does_not_credit_late_touches():
    d=day([['14:50',10.2,10.3,10.1,10.2,100000],['14:55',10.,10.2,9.9,10.,100000]])
    assert replay([d],10.,100,'fixed50','518880.SH')['filled']==0
    assert replay([d],10.,100,'fixed50','518880.SH',cutoff='15:01')['filled']==100


def test_zero_volume_placeholder_never_counts_as_a_fill():
    d=day([['09:35',10.,10.,9.,10.,0]])
    assert replay([d],10.,100,'fixed50','513100.SH')['filled']==0


def test_rolling_limit_cannot_overspend_initial_cash():
    d=day([['10:35',12.,12.1,11.9,12.,1000000]],previous_close=12.)
    result=replay([d],10.,900,'rolling50','513100.SH')
    assert result['filled']==800


def test_volume_capacity_is_in_shares_and_rounded_to_lots():
    d=day([['10:35',10.,10.1,9.9,10.,15000]])
    assert replay([d],10.,900,'fixed50','513100.SH')['filled']==100
