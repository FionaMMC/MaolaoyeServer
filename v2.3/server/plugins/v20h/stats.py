"""
统计计算 — Sharpe / Calmar / 调仓期 t-test / Newey-West / 年度分解
"""
import numpy as np
import pandas as pd
from scipy import stats as sst


def compute_full_stats(log_df: pd.DataFrame, capital_init: float, rebal_freq: int = 42) -> dict:
    """
    完整统计计算.

    Args:
        log_df: 必须有列 [date, equity, ret, idx_ret, excess]
        capital_init: 初始资金
        rebal_freq: 调仓周期 (用于调仓期 t-test)
    """
    if len(log_df) < 10:
        return {'error': 'too few days'}

    n_days = len(log_df)
    ny = n_days / 252

    # 总收益
    tot = log_df['equity'].iloc[-1] / capital_init - 1
    ann = (1 + tot) ** (1/ny) - 1 if tot > -1 else -1

    # 指数年化
    if 'idx_ret' in log_df.columns:
        idx_cum = (1 + log_df['idx_ret']).cumprod().iloc[-1] - 1
        idx_ann = (1 + idx_cum) ** (1/ny) - 1 if idx_cum > -1 else 0
    else:
        idx_ann = 0

    excess_ann = ann - idx_ann

    # Sharpe / IR
    dr = log_df['ret'].values
    er = log_df['excess'].values if 'excess' in log_df.columns else dr

    sh = np.mean(dr)/np.std(dr)*np.sqrt(252) if np.std(dr) > 0 else 0
    ir = np.mean(er)/np.std(er)*np.sqrt(252) if np.std(er) > 0 else 0

    # 回撤
    pk = log_df['equity'].cummax()
    dd = ((log_df['equity'] - pk) / pk).min()

    if 'excess' in log_df.columns:
        ec = (1 + log_df['excess']).cumprod()
        ep = ec.cummax()
        edd = ((ec - ep) / ep).min()
    else:
        edd = 0

    # 调仓期 t-test
    n_p = n_days // rebal_freq
    if n_p >= 3 and 'excess' in log_df.columns:
        prs = [np.prod(1+er[i*rebal_freq:(i+1)*rebal_freq])-1 for i in range(n_p)]
        t_r, p_r = sst.ttest_1samp(prs, 0)
    else:
        t_r, p_r = 0, 1

    # 日度 t-test
    if len(er) > 0 and np.std(er) > 0:
        t_d, p_d = sst.ttest_1samp(er, 0)
    else:
        t_d, p_d = 0, 1

    # Newey-West
    try:
        import statsmodels.api as sm
        if len(er) > 0:
            m = sm.OLS(er, np.ones(len(er))).fit(
                cov_type='HAC', cov_kwds={'maxlags': rebal_freq})
            t_n, p_n = float(m.tvalues[0]), float(m.pvalues[0])
        else:
            t_n, p_n = 0, 1
    except Exception:
        t_n, p_n = 0, 1

    # 月胜率
    df2 = log_df.copy()
    df2['month'] = pd.to_datetime(df2['date']).dt.to_period('M')
    if 'excess' in log_df.columns:
        me = df2.groupby('month')['excess'].sum()
    else:
        me = df2.groupby('month')['ret'].sum()
    mwr = float((me > 0).mean())

    # 年度分解
    df2['year'] = pd.to_datetime(df2['date']).dt.year
    year_stats = {}
    for yr in sorted(df2['year'].unique()):
        g = df2[df2['year'] == yr]
        s_ret = g['equity'].iloc[-1] / g['equity'].iloc[0] - 1
        if 'idx_ret' in g.columns:
            i_ret = (1 + g['idx_ret']).cumprod().iloc[-1] - 1
        else:
            i_ret = 0
        g_pk = g['equity'].cummax()
        y_dd = ((g['equity'] - g_pk) / g_pk).min()
        year_stats[int(yr)] = {
            'n_days': len(g),
            'strat_ret': float(s_ret),
            'idx_ret': float(i_ret),
            'excess': float(s_ret - i_ret),
            'dd': float(y_dd),
        }

    return {
        'n_days': n_days,
        'ann_return': float(ann),
        'idx_ann': float(idx_ann),
        'excess_ann': float(excess_ann),
        'sharpe': float(sh),
        'ir': float(ir),
        'max_dd': float(dd),
        'excess_dd': float(edd),
        'mwr': mwr,
        't_daily': float(t_d), 'p_daily': float(p_d),
        't_rebal': float(t_r), 'p_rebal': float(p_r),
        't_nw': float(t_n), 'p_nw': float(p_n),
        'calmar': float(abs(excess_ann / dd)) if dd != 0 else 0,
        'year_stats': year_stats,
    }


def print_stats(stats: dict, config_name: str = ""):
    """格式化打印统计指标"""
    if 'error' in stats:
        print(f"❌ {stats['error']}")
        return

    print(f"\n  {'═'*60}")
    if config_name:
        print(f"  {config_name}")
        print(f"  {'─'*60}")
    print(f"  样本天数:         {stats['n_days']}")
    print(f"  年化总收益:        {stats['ann_return']:>+7.2%}")
    print(f"  指数年化:         {stats['idx_ann']:>+7.2%}")
    print(f"  超额 α:           {stats['excess_ann']:>+7.2%}")
    print(f"  Sharpe:          {stats['sharpe']:>7.2f}")
    print(f"  IR:              {stats['ir']:>7.2f}")
    print(f"  最大回撤:         {stats['max_dd']:>+7.2%}")
    print(f"  超额回撤:         {stats['excess_dd']:>+7.2%}")
    print(f"  Calmar (α/|DD|): {stats['calmar']:>7.2f}")
    print(f"  月胜率:           {stats['mwr']:>7.1%}")

    sig_d = '***' if stats['p_daily']<0.01 else ('**' if stats['p_daily']<0.05 else ('*' if stats['p_daily']<0.10 else ''))
    sig_r = '***' if stats['p_rebal']<0.01 else ('**' if stats['p_rebal']<0.05 else ('*' if stats['p_rebal']<0.10 else ''))
    sig_n = '***' if stats['p_nw']<0.01 else ('**' if stats['p_nw']<0.05 else ('*' if stats['p_nw']<0.10 else ''))

    print(f"  日度 t/p:         {stats['t_daily']:>+5.2f} / {stats['p_daily']:.4f}{sig_d}")
    print(f"  调仓期 t/p:        {stats['t_rebal']:>+5.2f} / {stats['p_rebal']:.4f}{sig_r}")
    print(f"  Newey-West t/p:    {stats['t_nw']:>+5.2f} / {stats['p_nw']:.4f}{sig_n}")
    print(f"  {'─'*60}")
    print(f"  年度分解:")
    for yr, ys in stats['year_stats'].items():
        print(f"    {yr}: 策略 {ys['strat_ret']:>+7.2%}, 指数 {ys['idx_ret']:>+7.2%}, "
              f"α {ys['excess']:>+7.2%}, DD {ys['dd']:>+7.2%}")
    print(f"  {'═'*60}\n")
