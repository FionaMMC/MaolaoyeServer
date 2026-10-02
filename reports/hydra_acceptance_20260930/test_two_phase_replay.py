import pandas as pd
import pytest

from two_phase_replay import next_pair, run


def inputs():
    dates = pd.to_datetime(['20260923', '20260924', '20260928', '20260929',
                            '20260930', '20261008', '20261009', '20261012', '20261013'])
    close = pd.DataFrame({'X': 10.0, 'Y': 10.0}, index=dates)
    bars = {(d, s): {'open': 10., 'close': 10., 'low': 9., 'high': 11.,
                     'volume': 1_000_000, 'suspendFlag': 0}
            for d in dates for s in close.columns}
    weights = pd.DataFrame({'X': [1., 0.], 'Y': [0., 1.]}, index=dates[[0, 4]])
    return weights, bars, close


def test_holiday_roll_moves_both_legs_and_never_trades_signal_close():
    w, b, c = inputs()
    summary, hist, cycles, fills = run(w, b, c, [], capital=10000, slip_bps=0)
    assert list(cycles.sell_date) == list(pd.to_datetime(['20260928', '20261008']))
    assert list(cycles.buy_date) == list(pd.to_datetime(['20260929', '20261009']))
    assert list(fills.side) == ['BUY', 'SELL', 'BUY']
    assert list(fills.date) == list(pd.to_datetime(['20260929', '20261008', '20261009']))
    assert (fills.date > fills.signal_date).all()
    assert hist.cash.min() >= 0
    assert summary['modeled_notional_completion'] == 1
    assert summary['total_return'] == pytest.approx(-15 / 10000)


def test_gap_buy_uses_next_open_plus_slippage_never_yesterday_close():
    w, b, c = inputs()
    b[(pd.Timestamp('20260929'), 'X')]['open'] = 12
    b[(pd.Timestamp('20260929'), 'X')]['high'] = 13
    _, _, _, fills = run(w.iloc[:1], b, c, [], capital=10000, slip_bps=50)
    assert fills.iloc[0].price == pytest.approx(12.060)
    assert fills.iloc[0].quantity == 800
    assert fills.iloc[0].cash_after >= 0
    _, _, _, old_fills = run(w.iloc[:1], b, c, [], capital=10000, slip_bps=50,
                            buy_policy='previous_close_limit')
    assert old_fills.empty


def test_failed_sell_cannot_finance_next_day_buy():
    w, b, c = inputs()
    b[(pd.Timestamp('20261008'), 'X')]['suspendFlag'] = 1
    _, h, _, f = run(w, b, c, [], capital=10000, slip_bps=0)
    assert f.side.tolist() == ['BUY']
    assert h.cash.min() == 995


def test_cash_dividend_and_split_are_not_counted_as_profit_twice():
    w, b, c = inputs()
    ex, pay, split = pd.to_datetime(['20260930', '20261008', '20261009'])
    c.loc[c.index >= ex, 'X'] = 9
    c.loc[c.index >= split, 'X'] = 4.5
    for d in c.index:
        b[(d, 'X')]['close'] = c.loc[d, 'X']
    actions = [dict(symbol='X', record_date=pd.Timestamp('20260929'), ex_date=ex,
                    pay_date=pay, cash=1., factor=1.),
               dict(symbol='X', record_date=None, ex_date=split, pay_date=None, cash=0., factor=2.)]
    _, h, _, _ = run(w.iloc[:1], b, c, actions, capital=10000, slip_bps=0)
    assert h.loc[ex, 'receivable'] == 900
    assert h.loc[pay, 'cash'] == 1895 and h.loc[pay, 'receivable'] == 0
    assert h.loc[split, 'marked_holdings'] == 8100
    assert (h.loc['20260929':, 'nav'] == 9995).all()


def test_insufficient_calendar_never_invents_trading_day():
    w, b, c = inputs()
    assert next_pair(pd.to_datetime(['20260930', '20261008']), pd.Timestamp('20260930')) == (None, None)
    summary, _, _, fills = run(w.iloc[:1], b, c.iloc[:2], [], capital=10000)
    assert fills.empty and summary['unexecuted_windows'] == 1


def test_unlisted_unused_asset_never_gets_fabricated_price_or_fill():
    w, b, c = inputs()
    w = w.iloc[:1]
    c['Y'] = float('nan')
    _, _, _, fills = run(w, b, c, [], capital=10000, slip_bps=0)
    assert set(fills.symbol) == {'X'}
    w.loc[:, 'X'] = 0
    w.loc[:, 'Y'] = 1
    with pytest.raises(ValueError, match='missing required'):
        run(w, b, c, [], capital=10000)
