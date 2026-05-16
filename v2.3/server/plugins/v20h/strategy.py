"""
V20-H Production V2 Strategy
=================================
CUT10 + V12 期货对冲 (graduated_4) + 42天调仓 + 1.5x cap-weight + Vol Target 15%

真实可实盘 α = +12.50%, Sharpe 1.12, DD -17.22% (1000万-1亿资金)
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable
import numpy as np
import pandas as pd


@dataclass
class StrategyConfig:
    """V20-H Production V2 配置"""
    # 资金
    capital_init: float = 10_000_000

    # 选股
    cut_pct: float = 0.10           # 剔除底部 10%
    rebal_freq: int = 42            # 42 天调仓
    weight_cap: float = 1.5         # 单股权重上限 = 1.5 × 1/N

    # V12 阈值 (graduated_4)
    q10_quantile: float = 0.10
    q20_quantile: float = 0.20
    q40_quantile: float = 0.40
    q_warmup_days: int = 180

    # Vol targeting
    use_vol_target: bool = True
    target_vol_ann: float = 0.15
    vol_lookback: int = 20

    # 股票成本
    stock_cmn_rate: float = 0.0003
    min_stock_cmn: float = 5.0
    stamp_duty: float = 0.0005
    bond_yield: float = 0.035

    # 期货成本
    fut_cmn_rate: float = 0.0005
    basis_cost: float = 0.03
    fut_margin_ratio: float = 0.15
    roll_cost_bps: float = 10

    # 执行
    lot_size: int = 100
    cash_buffer: float = 0.02
    start_date: str = "2021-04-01"

    # 账户权限：剔除账户不能交易的板块前缀
    # 默认排除科创板 688/689——QMT 普通账户无 50 万资产 + 科创板权限
    # 实盘历史中 V20H 因此累计 43 个 688 票进黑名单
    excluded_symbol_prefixes: tuple = ("688", "689")


def compute_expanding_quantiles(v12: pd.Series, start: str = '2021-04-01',
                                 quantiles: List[float] = [0.10, 0.20, 0.40],
                                 warmup: int = 180) -> Dict[float, pd.Series]:
    """计算 expanding window 的多个分位数阈值 (无前瞻)"""
    dates = sorted(v12[v12.index >= pd.Timestamp(start)].index)
    result = {}
    for q in quantiles:
        result[q] = pd.Series({
            d: v12[v12.index < d].quantile(q) if len(v12[v12.index < d]) >= warmup else 0.30
            for d in dates
        })
    return result


def graduated_4_hedge(v12_val: float, q10: float, q20: float, q40: float) -> float:
    """
    Graduated_4 对冲规则:
      V12 < Q10 → hedge 100%
      V12 < Q20 → hedge 70%
      V12 < Q40 → hedge 30%
      else      → hedge 0%
    """
    if v12_val < q10: return 1.0
    elif v12_val < q20: return 0.7
    elif v12_val < q40: return 0.3
    else: return 0.0


class V20HStrategy:
    """
    V20-H Production V2 策略实现.
    可用于回测 / 实盘信号生成.
    """

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.reset()

    def reset(self):
        """重置状态, 用于新回测"""
        self.cash = self.cfg.capital_init
        self.positions: Dict[str, int] = {}
        self.prices: Dict[str, float] = {}
        self.fut_short = 0.0
        self.fut_margin = 0.0
        self.fut_mtm = 0.0
        self.prev_hedge = 0.0
        self.last_rb_idx = -self.cfg.rebal_freq
        self.equity_history = [self.cfg.capital_init]
        self.daily_rets = []

        # 计数
        self.n_main = 0
        self.n_cap = 0
        self.n_fut = 0
        self.cost_stk_cmn = 0.0
        self.cost_stk_duty = 0.0
        self.cost_fut_cmn = 0.0
        self.cost_basis = 0.0

    @property
    def stock_value(self) -> float:
        return sum(self.positions[c] * self.prices.get(c, 0)
                   for c in self.positions if c in self.prices)

    @property
    def total_equity(self) -> float:
        return self.cash + self.stock_value + self.fut_mtm

    def daily_costs(self, prev_idx_price: Optional[float], cur_idx_price: float,
                    is_roll_day: bool = False):
        """日度: 基差 + MTM + 利息 + 展期"""
        if prev_idx_price is None or cur_idx_price is None:
            return

        # 基差成本
        if self.fut_short > 0:
            basis_d = self.fut_short * (self.cfg.basis_cost / 252)
            self.cash -= basis_d
            self.cost_basis += basis_d

            # MTM
            idx_ret = (cur_idx_price - prev_idx_price) / prev_idx_price
            mtm = -self.fut_short * idx_ret
            self.fut_mtm += mtm
            self.fut_short *= (cur_idx_price / prev_idx_price)
            self.fut_margin = self.fut_short * self.cfg.fut_margin_ratio

        # 现金利息 (扣除保证金占用)
        cash_avail = max(0, self.cash - self.fut_margin)
        self.cash += cash_avail * (self.cfg.bond_yield / 252)

        # 季度展期
        if is_roll_day and self.fut_short > 0:
            roll_c = self.fut_short * (self.cfg.roll_cost_bps / 10000)
            self.cash -= roll_c
            self.cost_fut_cmn += roll_c
            self.n_fut += 1

    def compute_target_hedge(self, v12_val: float,
                             q10: float, q20: float, q40: float) -> float:
        """计算目标对冲比例"""
        return graduated_4_hedge(v12_val, q10, q20, q40)

    def compute_vol_scale(self) -> float:
        """波动率 targeting: 计算仓位 scale"""
        if not self.cfg.use_vol_target:
            return 1.0
        if len(self.daily_rets) < self.cfg.vol_lookback:
            return 1.0

        recent = np.array(self.daily_rets[-self.cfg.vol_lookback:])
        realized_vol_ann = np.std(recent) * np.sqrt(252)

        if realized_vol_ann > self.cfg.target_vol_ann:
            scale = self.cfg.target_vol_ann / realized_vol_ann
        else:
            scale = 1.0
        return max(0.3, min(1.0, scale))

    def apply_cap_weight(self):
        """单股权重上限检查 + 砍掉超出部分"""
        if not self.cfg.weight_cap or not self.positions:
            return

        sv = {c: self.positions[c] * self.prices.get(c, 0) for c in self.positions}
        tot_sv = sum(sv.values())
        if tot_sv <= 0:
            return

        cap_limit = (tot_sv / len(self.positions)) * self.cfg.weight_cap

        for code, val in list(sv.items()):
            if val > cap_limit:
                price = self.prices.get(code, 0)
                if price <= 0:
                    continue
                excess = val - cap_limit
                shares_to_sell = int(excess / price // self.cfg.lot_size) * self.cfg.lot_size
                if shares_to_sell <= 0:
                    continue
                amt = shares_to_sell * price
                cmn = max(self.cfg.min_stock_cmn, amt * self.cfg.stock_cmn_rate)
                duty = amt * self.cfg.stamp_duty
                self.cash += amt - cmn - duty
                self.positions[code] -= shares_to_sell
                if self.positions[code] == 0:
                    del self.positions[code]
                self.cost_stk_cmn += cmn
                self.cost_stk_duty += duty
                self.n_cap += 1

    def rebalance_stocks(self, target_codes: List[str], vol_scale: float = 1.0):
        """主调仓: 卖出非目标 + 买入目标 (含 vol_scale)"""
        if not target_codes:
            return

        target_set = set(target_codes)
        current = set(self.positions.keys())

        # 卖出非目标
        for code in current - target_set:
            shares = self.positions[code]
            price = self.prices.get(code, 0)
            if price <= 0 or shares <= 0:
                continue
            amt = shares * price
            cmn = max(self.cfg.min_stock_cmn, amt * self.cfg.stock_cmn_rate)
            duty = amt * self.cfg.stamp_duty
            self.cash += amt - cmn - duty
            del self.positions[code]
            self.cost_stk_cmn += cmn
            self.cost_stk_duty += duty
            self.n_main += 1

        # 买入新目标 (按 vol_scale 缩放)
        to_buy = target_set - current
        if to_buy:
            avail = self.cash * (1 - self.cfg.cash_buffer) * vol_scale
            per_stock = avail / len(to_buy)

            for code in to_buy:
                price = self.prices.get(code, 0)
                if price <= 0:
                    continue
                shares = int(per_stock / price // self.cfg.lot_size) * self.cfg.lot_size
                if shares <= 0:
                    continue
                amt = shares * price
                cmn = max(self.cfg.min_stock_cmn, amt * self.cfg.stock_cmn_rate)
                if self.cash >= amt + cmn:
                    self.cash -= amt + cmn
                    self.positions[code] = shares
                    self.cost_stk_cmn += cmn
                    self.n_main += 1

    def adjust_hedge(self, target_hedge: float, cur_idx_price: float):
        """调整期货空头到目标对冲比例"""
        if cur_idx_price is None or cur_idx_price <= 0:
            return

        if abs(target_hedge - self.prev_hedge) <= 0.05:
            return  # 5% 阈值, 避免频繁微调

        stock_v = self.stock_value
        target_notional = stock_v * target_hedge
        delta = target_notional - self.fut_short

        if abs(delta) >= 100_000:
            self.fut_short += delta
            self.fut_margin = self.fut_short * self.cfg.fut_margin_ratio
            cmn = max(1, abs(delta) * self.cfg.fut_cmn_rate)
            self.cash -= cmn
            self.cost_fut_cmn += cmn
            self.n_fut += 1

        self.prev_hedge = target_hedge

    def step(self, date: pd.Timestamp, prices_today: Dict[str, float],
             cur_idx_price: float, prev_idx_price: Optional[float],
             v12_val: float, q10: float, q20: float, q40: float,
             pred_today: pd.DataFrame, di: int, is_roll_day: bool = False) -> Dict:
        """
        每日单步: 接收输入, 输出当日动作和组合状态.

        Args:
            date: 当日日期
            prices_today: {code: close_price} 今日股票收盘价
            cur_idx_price: 今日 CSI1000 收盘价
            prev_idx_price: 昨日 CSI1000 收盘价 (None 则首日)
            v12_val: 今日 V12 exposure 值
            q10, q20, q40: 今日 V12 expanding 分位数阈值
            pred_today: 今日预测 DataFrame, 列 [code, prob_top]
            di: 当前日期索引 (0-based, 用于 rebal_freq 判断)
            is_roll_day: 是否为期货展期日

        Returns:
            日志字典
        """
        # 1. 更新价格
        self.prices.update(prices_today)

        # 2. 日度成本
        self.daily_costs(prev_idx_price, cur_idx_price, is_roll_day)

        # 3. 当前净值 + 历史
        cur_equity = self.total_equity
        self.equity_history.append(cur_equity)
        if len(self.equity_history) >= 2:
            self.daily_rets.append(
                (self.equity_history[-1] - self.equity_history[-2]) / self.equity_history[-2]
            )

        # 4. 计算对冲目标
        target_hedge = self.compute_target_hedge(v12_val, q10, q20, q40)

        # 5. 计算 vol_scale
        vol_scale = self.compute_vol_scale()

        # 6. Cap-weight
        self.apply_cap_weight()

        # 7. 主调仓
        if di - self.last_rb_idx >= self.cfg.rebal_freq and len(pred_today) >= 20:
            n_cut = max(1, int(len(pred_today) * self.cfg.cut_pct))
            bottom = set(pred_today.nsmallest(n_cut, 'prob_top')['code'])
            target_codes = pred_today[~pred_today['code'].isin(bottom)]['code'].tolist()
            target_codes = [c for c in target_codes if c in self.prices and self.prices[c] > 0]
            self.rebalance_stocks(target_codes, vol_scale)
            self.last_rb_idx = di

        # 8. 期货对冲
        self.adjust_hedge(target_hedge, cur_idx_price)

        return {
            'date': date,
            'equity': cur_equity,
            'cash': self.cash,
            'stock_value': self.stock_value,
            'fut_short': self.fut_short,
            'fut_mtm': self.fut_mtm,
            'hedge_ratio': self.prev_hedge,
            'target_hedge': target_hedge,
            'vol_scale': vol_scale,
            'v12': v12_val,
            'q10': q10, 'q20': q20, 'q40': q40,
            'n_positions': len(self.positions),
        }

    def get_total_cost(self) -> float:
        return (self.cost_stk_cmn + self.cost_stk_duty +
                self.cost_fut_cmn + self.cost_basis)
