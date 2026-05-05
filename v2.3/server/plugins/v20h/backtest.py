"""
回测主循环 — 用 V20HStrategy 对历史数据做完整回测
"""
from typing import Dict, List
import pandas as pd
import numpy as np
from .strategy import V20HStrategy, StrategyConfig, compute_expanding_quantiles
from .stats import compute_full_stats


def get_roll_days(dates: List[pd.Timestamp]) -> set:
    """季度展期日 (3/6/9/12 月 20 日后第一个交易日)"""
    roll_days = set()
    for yr in range(2018, 2030):
        for m in [3, 6, 9, 12]:
            target = pd.Timestamp(f"{yr}-{m}-20")
            future = [d for d in dates if d >= target]
            if future:
                roll_days.add(future[0])
    return roll_days


def run_backtest(data: Dict, cfg: StrategyConfig) -> Dict:
    """
    完整回测.

    Args:
        data: DataLoader.load_all() 返回的字典
        cfg: StrategyConfig

    Returns:
        {
            'log': pd.DataFrame  逐日组合状态,
            'stats': dict        统计指标,
            'config': dict       配置参数,
            'cost_breakdown': dict  成本明细,
        }
    """
    pred = data['pred']
    v12 = data['v12']
    close_df = data['close_df']
    idx = data['idx']

    # 计算 expanding 分位数
    q_thresh = compute_expanding_quantiles(
        v12, start=cfg.start_date,
        quantiles=[cfg.q10_quantile, cfg.q20_quantile, cfg.q40_quantile],
        warmup=cfg.q_warmup_days,
    )

    # 日期范围
    dates = sorted(pred['date'].unique())
    dates = [d for d in dates if d in close_df.index and d >= pd.Timestamp(cfg.start_date)]
    roll_days = get_roll_days(dates)

    # 初始化 strategy
    strategy = V20HStrategy(cfg)

    # 主循环
    log = []
    prev_idx = None

    for di, date in enumerate(dates):
        # 当日价格
        prices_today = (close_df.loc[date].dropna().to_dict()
                        if date in close_df.index else {})
        cur_idx_p = idx.loc[date, 'close'] if date in idx.index else prev_idx

        # V12 信号
        v12_val = v12.get(date, 0.5)
        q10 = q_thresh[cfg.q10_quantile].get(date, 0.30)
        q20 = q_thresh[cfg.q20_quantile].get(date, 0.30)
        q40 = q_thresh[cfg.q40_quantile].get(date, 0.30)

        # 当日预测
        pred_today = pred[pred['date'] == date]

        # 单步执行
        log_entry = strategy.step(
            date=date,
            prices_today=prices_today,
            cur_idx_price=cur_idx_p,
            prev_idx_price=prev_idx,
            v12_val=v12_val,
            q10=q10, q20=q20, q40=q40,
            pred_today=pred_today,
            di=di,
            is_roll_day=(date in roll_days),
        )
        log.append(log_entry)

        prev_idx = cur_idx_p

    # 转 DataFrame + 计算指数收益
    log_df = pd.DataFrame(log)
    log_df['ret'] = log_df['equity'].pct_change().fillna(0)

    # 计算指数日收益 (对齐 log 日期)
    idx_returns = idx['close'].pct_change()
    idx_ret_list = []
    for d in log_df['date']:
        if d in idx_returns.index:
            r = idx_returns.loc[d]
            idx_ret_list.append(0.0 if pd.isna(r) else float(r))
        else:
            idx_ret_list.append(0.0)
    log_df['idx_ret'] = idx_ret_list
    log_df['excess'] = log_df['ret'] - log_df['idx_ret']

    # 计算统计
    stats = compute_full_stats(log_df, cfg.capital_init, cfg.rebal_freq)

    # 成本明细
    total_cost = strategy.get_total_cost()
    cost_breakdown = {
        'stock_commission': strategy.cost_stk_cmn,
        'stamp_duty': strategy.cost_stk_duty,
        'futures_commission': strategy.cost_fut_cmn,
        'basis_cost': strategy.cost_basis,
        'total': total_cost,
        'pct_of_capital': total_cost / cfg.capital_init,
        'annualized': (total_cost / cfg.capital_init) / (len(log_df) / 252),
    }

    return {
        'log': log_df,
        'stats': stats,
        'config': cfg,
        'cost_breakdown': cost_breakdown,
        'trade_counts': {
            'main_stock_trades': strategy.n_main,
            'cap_weight_trades': strategy.n_cap,
            'futures_trades': strategy.n_fut,
        },
    }
