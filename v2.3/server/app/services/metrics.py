"""量化分析指标服务。

输入：perf_snapshots (instance_id, date, nav, daily_return)
输出：Sharpe / Sortino / MaxDD / Calmar / 胜率 / 年化 / Beta / Alpha / IR / 等

所有计算纯函数，无 I/O；从 SQLAlchemy 取数据走 PerfQuery 包装。

设计原则：
- 数值上和 numpy / pandas 行为一致（用 math/statistics，避免依赖）
- 没数据返回 None 而不是 0，前端再决定怎么显示
- 用 252 个交易日年化，rf=0.035（与 V20H bond_yield 一致）
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy import func, select

from app.models import (
    ExecutionQualityObservation,
    Order,
    OrderSignalMap,
    PerfSnapshot,
    RawSignal,
    ShadowNavSnapshot,
    Trade,
)

TRADING_DAYS = 252
RISK_FREE_RATE = 0.035  # 年化无风险，和 V20H bond_yield 一致


# ── 纯函数：统计基础 ─────────────────────────────────────────────────────
def _mean(xs: Sequence[float]) -> float | None:
    if not xs:
        return None
    return sum(xs) / len(xs)


def _stdev(xs: Sequence[float], ddof: int = 1) -> float | None:
    n = len(xs)
    if n - ddof <= 0:
        return None
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(var)


def _cov(xs: Sequence[float], ys: Sequence[float], ddof: int = 1) -> float | None:
    n = len(xs)
    if n - ddof <= 0 or n != len(ys):
        return None
    mx, my = _mean(xs), _mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n - ddof)


# ── 核心指标计算 ────────────────────────────────────────────────────────
@dataclass
class PerfSummary:
    """单个区间的综合表现摘要。所有 None 表示样本不够算。"""
    start_date: str | None = None
    end_date: str | None = None
    n_days: int = 0

    # 收益
    cumulative_return: float | None = None
    annualized_return: float | None = None
    total_pnl: float | None = None

    # 风险
    annualized_volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    max_drawdown_duration_days: int | None = None
    calmar: float | None = None
    var_95: float | None = None  # 历史 VaR 95%

    # 胜负
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    profit_factor: float | None = None

    # 当前 / 终值
    start_nav: float | None = None
    end_nav: float | None = None
    peak_nav: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def compute_summary(
    rows: list[tuple[str, float, float | None]],
    rf_annual: float = RISK_FREE_RATE,
) -> PerfSummary:
    """rows = [(date_str, nav, daily_return_or_None)]，按时间升序。"""
    if not rows:
        return PerfSummary()

    rows = sorted(rows, key=lambda r: r[0])
    n = len(rows)
    start_nav = rows[0][1]
    end_nav = rows[-1][1]

    summary = PerfSummary(
        start_date=rows[0][0],
        end_date=rows[-1][0],
        n_days=n,
        start_nav=float(start_nav),
        end_nav=float(end_nav),
        peak_nav=float(max(r[1] for r in rows)),
        total_pnl=float(end_nav - start_nav),
    )

    # 累计收益
    if start_nav > 0:
        summary.cumulative_return = (end_nav - start_nav) / start_nav
        # 年化（按交易日）
        if n > 1:
            summary.annualized_return = (
                (end_nav / start_nav) ** (TRADING_DAYS / n) - 1
            )

    # 用 daily_return 算波动率 / Sharpe 等
    daily_rets = [r[2] for r in rows if r[2] is not None]
    if len(daily_rets) >= 2:
        sd = _stdev(daily_rets)
        if sd is not None and sd > 0:
            summary.annualized_volatility = sd * math.sqrt(TRADING_DAYS)
            mean_excess = _mean(daily_rets) - rf_annual / TRADING_DAYS
            summary.sharpe = (mean_excess / sd) * math.sqrt(TRADING_DAYS)

        # Sortino：只用下行波动
        downsides = [r for r in daily_rets if r < 0]
        if len(downsides) >= 2:
            ds_sd = _stdev(downsides)
            if ds_sd is not None and ds_sd > 0:
                mean_excess = _mean(daily_rets) - rf_annual / TRADING_DAYS
                summary.sortino = (mean_excess / ds_sd) * math.sqrt(TRADING_DAYS)

        # 胜率 + 盈亏比
        wins = [r for r in daily_rets if r > 0]
        losses = [r for r in daily_rets if r < 0]
        summary.win_rate = len(wins) / len(daily_rets)
        if wins:
            summary.avg_win = _mean(wins)
        if losses:
            summary.avg_loss = _mean(losses)
        if wins and losses:
            total_w = sum(wins)
            total_l = abs(sum(losses))
            summary.profit_factor = total_w / total_l if total_l > 0 else None

        # 历史 VaR 95% (1-day): 5% 分位数
        if len(daily_rets) >= 20:
            sorted_rets = sorted(daily_rets)
            idx = max(0, int(0.05 * len(sorted_rets)) - 1)
            summary.var_95 = sorted_rets[idx]

    # 最大回撤（基于 nav）
    navs = [r[1] for r in rows]
    dates = [r[0] for r in rows]
    mdd, dd_days = _compute_max_drawdown(navs, dates)
    summary.max_drawdown = mdd
    summary.max_drawdown_duration_days = dd_days

    # Calmar = 年化 / |MDD|
    if (summary.annualized_return is not None
            and summary.max_drawdown is not None
            and summary.max_drawdown < 0):
        summary.calmar = summary.annualized_return / abs(summary.max_drawdown)

    return summary


def _compute_max_drawdown(
    navs: list[float], dates: list[str]
) -> tuple[float | None, int | None]:
    """返回 (max_dd, dd_duration_days)。max_dd 是负数；duration = 从峰值到谷底的天数。"""
    if len(navs) < 2:
        return None, None

    peak = navs[0]
    peak_idx = 0
    max_dd = 0.0
    max_dd_duration = 0
    for i, nav in enumerate(navs):
        if nav > peak:
            peak = nav
            peak_idx = i
        else:
            dd = (nav - peak) / peak if peak > 0 else 0
            if dd < max_dd:
                max_dd = dd
                max_dd_duration = i - peak_idx

    return max_dd, max_dd_duration


def compute_drawdown_series(
    navs: list[float],
) -> list[float]:
    """返回每个时点的 drawdown（相对历史最高点）。"""
    if not navs:
        return []
    out = []
    peak = navs[0]
    for nav in navs:
        if nav > peak:
            peak = nav
        out.append((nav - peak) / peak if peak > 0 else 0.0)
    return out


# ── 收益序列对比（vs 基准） ──────────────────────────────────────────────
@dataclass
class BenchmarkComparison:
    benchmark_name: str = ""
    n_days: int = 0
    beta: float | None = None
    alpha_annual: float | None = None
    correlation: float | None = None
    tracking_error: float | None = None
    information_ratio: float | None = None
    portfolio_return: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def compute_benchmark_comparison(
    port_rets: Sequence[float],
    bench_rets: Sequence[float],
    rf_annual: float = RISK_FREE_RATE,
    benchmark_name: str = "CSI1000",
) -> BenchmarkComparison:
    """两个等长收益率序列对比。"""
    if len(port_rets) != len(bench_rets) or len(port_rets) < 5:
        return BenchmarkComparison(benchmark_name=benchmark_name, n_days=len(port_rets))

    out = BenchmarkComparison(benchmark_name=benchmark_name, n_days=len(port_rets))

    out.portfolio_return = sum(port_rets)
    out.benchmark_return = sum(bench_rets)
    out.excess_return = out.portfolio_return - out.benchmark_return

    # Beta = Cov(port, bench) / Var(bench)
    var_b = _stdev(bench_rets)
    if var_b is not None and var_b > 0:
        cov = _cov(port_rets, bench_rets)
        if cov is not None:
            out.beta = cov / (var_b ** 2)

        # Correlation
        sd_p = _stdev(port_rets)
        if sd_p is not None and sd_p > 0 and cov is not None:
            out.correlation = cov / (sd_p * var_b)

    # Alpha (CAPM, annualized): R_p - rf = α + β(R_b - rf)
    if out.beta is not None:
        mean_p = _mean(port_rets) * TRADING_DAYS
        mean_b = _mean(bench_rets) * TRADING_DAYS
        out.alpha_annual = (mean_p - rf_annual) - out.beta * (mean_b - rf_annual)

    # Tracking Error = std(diff) * sqrt(252)
    diffs = [p - b for p, b in zip(port_rets, bench_rets)]
    te_d = _stdev(diffs)
    if te_d is not None and te_d > 0:
        out.tracking_error = te_d * math.sqrt(TRADING_DAYS)
        # IR = mean(excess) / te (annualized)
        mean_d_ann = _mean(diffs) * TRADING_DAYS
        out.information_ratio = mean_d_ann / out.tracking_error

    return out


# ── 时间窗口辅助 ────────────────────────────────────────────────────────
def date_range_for_period(period: str, today: datetime | None = None) -> str:
    """返回 cutoff_date（YYYYMMDD），period ∈ {7d, 30d, 90d, 180d, 1y, ytd, all}。"""
    if today is None:
        today = datetime.now()
    if period == "all":
        return "00000000"
    if period == "ytd":
        return f"{today.year}0101"
    delta_map = {
        "7d": 7, "30d": 30, "90d": 90, "180d": 180, "1y": 365,
    }
    days = delta_map.get(period)
    if days is None:
        return f"{today.year - 1}0101"
    return (today - timedelta(days=days)).strftime("%Y%m%d")


# ── DB 数据读取 ────────────────────────────────────────────────────────
class MetricsService:
    """从 SQLite 读正式或影子 NAV 快照及订单成交，组装成指标。"""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def load_perf_rows(
        self,
        instance_id: str,
        cutoff: str = "00000000",
    ) -> list[tuple[str, float, float | None]]:
        """返回 [(date, nav, daily_return)] 按时间升序。"""
        with self.session_factory() as session:
            if instance_id.startswith("Shadow_"):
                stmt = (
                    select(
                        ShadowNavSnapshot.date,
                        ShadowNavSnapshot.nav,
                        ShadowNavSnapshot.daily_return,
                    )
                    .where(ShadowNavSnapshot.shadow_id == instance_id)
                    .where(ShadowNavSnapshot.date >= cutoff)
                    .order_by(ShadowNavSnapshot.date)
                )
            else:
                stmt = (
                    select(PerfSnapshot.date, PerfSnapshot.nav, PerfSnapshot.daily_return)
                    .where(PerfSnapshot.instance_id == instance_id)
                    .where(PerfSnapshot.date >= cutoff)
                    .order_by(PerfSnapshot.date)
                )
            return [(r[0], float(r[1]), float(r[2]) if r[2] is not None else None)
                    for r in session.execute(stmt).all()]

    def summary(self, instance_id: str, period: str = "all") -> PerfSummary:
        cutoff = date_range_for_period(period)
        rows = self.load_perf_rows(instance_id, cutoff)
        return compute_summary(rows)

    def drawdown_series(self, instance_id: str, period: str = "all") -> dict:
        cutoff = date_range_for_period(period)
        rows = self.load_perf_rows(instance_id, cutoff)
        navs = [r[1] for r in rows]
        dates = [r[0] for r in rows]
        return {
            "dates": dates,
            "drawdown": compute_drawdown_series(navs),
            "nav": navs,
        }

    def periodic_returns(
        self,
        instance_id: str,
        period: str = "all",
        freq: str = "monthly",
    ) -> list[dict]:
        """按 weekly/monthly/yearly 聚合收益。

        freq=monthly → [{period: "2026-05", nav_start, nav_end, ret, n_days}]
        """
        cutoff = date_range_for_period(period)
        rows = self.load_perf_rows(instance_id, cutoff)
        if not rows:
            return []

        buckets: dict[str, list[tuple[str, float]]] = {}
        for date_str, nav, _ in rows:
            key = self._bucket_key(date_str, freq)
            buckets.setdefault(key, []).append((date_str, nav))

        result = []
        for key in sorted(buckets.keys()):
            bucket = sorted(buckets[key], key=lambda t: t[0])
            nav_start = bucket[0][1]
            nav_end = bucket[-1][1]
            result.append({
                "period": key,
                "n_days": len(bucket),
                "nav_start": nav_start,
                "nav_end": nav_end,
                "return": (nav_end - nav_start) / nav_start if nav_start > 0 else 0.0,
                "pnl": nav_end - nav_start,
            })
        return result

    @staticmethod
    def _bucket_key(date_str: str, freq: str) -> str:
        """date_str 'YYYYMMDD' → bucket key（YYYY-MM / YYYY-Www / YYYY）。"""
        if len(date_str) != 8:
            return date_str
        y, m = date_str[:4], date_str[4:6]
        if freq == "yearly":
            return y
        if freq == "monthly":
            return f"{y}-{m}"
        if freq == "weekly":
            try:
                dt = datetime.strptime(date_str, "%Y%m%d")
                iso_y, iso_w, _ = dt.isocalendar()
                return f"{iso_y}-W{iso_w:02d}"
            except Exception:
                return date_str
        return date_str  # daily / 默认

    def trade_analytics(
        self,
        account_group: str | None = None,
        cutoff: str = "00000000",
    ) -> dict:
        """订单 + 成交统计：总笔数、成功率、commission、turnover、avg fill 等。"""
        with self.session_factory() as session:
            base = select(Order).where(Order.valid_date >= cutoff)
            if account_group:
                base = base.where(Order.account_group == account_group)
            orders = session.execute(base).scalars().all()
            n_total = len(orders)
            status_count: Counter[str] = Counter(o.status for o in orders)
            dir_count: Counter[str] = Counter(o.direction for o in orders)

            # 真实成交（trades 表）
            order_ids = [o.order_id for o in orders]
            n_trades = 0
            sum_filled_amt = 0.0
            sum_filled_qty = 0
            if order_ids:
                # 分批 IN 查询（SQLite IN 上限）
                BATCH = 500
                for i in range(0, len(order_ids), BATCH):
                    chunk = order_ids[i:i + BATCH]
                    trades = session.execute(
                        select(Trade.filled_quantity, Trade.filled_price)
                        .where(Trade.order_id.in_(chunk))
                    ).all()
                    for q, p in trades:
                        n_trades += 1
                        sum_filled_amt += float(q) * float(p)
                        sum_filled_qty += int(q)

            # bookkeeping_divergence
            bk_div = sum(1 for o in orders if getattr(o, "bookkeeping_divergence", False))

        fill_rate = None
        terminal_count = status_count.get("FILLED", 0) + status_count.get("PARTIAL", 0)
        if n_total > 0:
            fill_rate = terminal_count / n_total

        return {
            "cutoff": cutoff,
            "account_group": account_group,
            "n_orders": n_total,
            "by_status": dict(status_count),
            "by_direction": dict(dir_count),
            "fill_rate": fill_rate,
            "n_trades": n_trades,
            "total_filled_amount": sum_filled_amt,
            "total_filled_quantity": sum_filled_qty,
            "bookkeeping_divergence_count": bk_div,
        }

    def execution_analysis(
        self,
        instance_id: str,
        cutoff: str = "00000000",
        limit: int = 200,
    ) -> dict:
        """Instance-scoped strategy-reference to actual-fill attribution.

        Orders are account-group aggregates, so instance ownership must be
        resolved through ``order_signal_map``.  If an aggregate ever contains
        multiple instances, fill quantity, notional, fees and implementation
        cost are allocated by the mapped signal-quantity share.  Prices and bp
        slippage remain the common execution price for that aggregate order.
        """
        with self.session_factory() as session:
            mapped_rows = session.execute(
                select(
                    OrderSignalMap.order_id,
                    OrderSignalMap.signal_quantity,
                    RawSignal.reference_price,
                )
                .join(RawSignal, RawSignal.signal_id == OrderSignalMap.signal_id)
                .where(RawSignal.instance_id == instance_id)
                .where(RawSignal.valid_date >= cutoff)
            ).all()

            instance_refs: dict[str, dict[str, float | int]] = {}
            for order_id, quantity, reference_price in mapped_rows:
                item = instance_refs.setdefault(
                    order_id,
                    {"quantity": 0, "weighted_reference": 0.0, "signals": 0},
                )
                item["quantity"] += int(quantity)
                item["weighted_reference"] += float(quantity) * float(reference_price)
                item["signals"] += 1

            order_ids = list(instance_refs)
            orders: dict[str, Order] = {}
            total_mapped_quantity: dict[str, int] = {}
            latest_fills: dict[str, Trade] = {}
            quality: dict[str, ExecutionQualityObservation] = {}
            batch_size = 500
            for offset in range(0, len(order_ids), batch_size):
                chunk = order_ids[offset:offset + batch_size]
                for order in session.execute(
                    select(Order).where(Order.order_id.in_(chunk))
                ).scalars():
                    orders[order.order_id] = order
                for order_id, quantity in session.execute(
                    select(
                        OrderSignalMap.order_id,
                        func.sum(OrderSignalMap.signal_quantity),
                    )
                    .where(OrderSignalMap.order_id.in_(chunk))
                    .group_by(OrderSignalMap.order_id)
                ):
                    total_mapped_quantity[order_id] = int(quantity)
                for trade in session.execute(
                    select(Trade)
                    .where(Trade.order_id.in_(chunk))
                    .where(Trade.filled_quantity > 0)
                    .order_by(Trade.id)
                ).scalars():
                    latest_fills[trade.order_id] = trade
                for observation in session.execute(
                    select(ExecutionQualityObservation).where(
                        ExecutionQualityObservation.order_id.in_(chunk)
                    )
                ).scalars():
                    quality[observation.order_id] = observation

        status_count: Counter[str] = Counter()
        direction_count: Counter[str] = Counter()
        details = []
        for order_id, ref in instance_refs.items():
            order = orders.get(order_id)
            if order is None or order.valid_date < cutoff:
                continue
            status_count[order.status] += 1
            direction_count[order.direction] += 1

            observation = quality.get(order_id)
            trade = latest_fills.get(order_id)
            filled_quantity = 0.0
            fill_price = None
            used_quality_fill = False
            if observation and observation.filled_quantity > 0 and observation.fill_vwap:
                filled_quantity = float(observation.filled_quantity)
                fill_price = float(observation.fill_vwap)
                used_quality_fill = True
            elif trade is not None:
                filled_quantity = float(trade.filled_quantity)
                fill_price = float(trade.filled_price)
            if filled_quantity <= 0 or fill_price is None or fill_price <= 0:
                continue

            instance_quantity = int(ref["quantity"])
            mapped_quantity = total_mapped_quantity.get(order_id) or order.quantity
            allocation_ratio = instance_quantity / mapped_quantity if mapped_quantity else 0.0
            allocated_fill_quantity = filled_quantity * allocation_ratio
            strategy_price = (
                float(ref["weighted_reference"]) / instance_quantity
                if instance_quantity else None
            )
            sign = 1.0 if order.direction == "BUY" else -1.0
            raw_price_difference = None
            strategy_to_fill_bps = None
            implementation_shortfall = None
            if strategy_price not in (None, 0):
                raw_price_difference = fill_price - strategy_price
                strategy_to_fill_bps = sign * (
                    fill_price / strategy_price - 1.0
                ) * 10_000
                implementation_shortfall = (
                    sign * raw_price_difference * allocated_fill_quantity
                )
                if abs(strategy_to_fill_bps) < 1e-6:
                    raw_price_difference = 0.0
                    strategy_to_fill_bps = 0.0
                    implementation_shortfall = 0.0
            arrival_price = (
                float(observation.arrival_reference_price)
                if observation and observation.arrival_reference_price is not None
                else None
            )
            strategy_to_arrival_bps = None
            arrival_to_fill_bps = None
            if strategy_price not in (None, 0) and arrival_price is not None:
                strategy_to_arrival_bps = sign * (
                    arrival_price / strategy_price - 1.0
                ) * 10_000
            if arrival_price not in (None, 0):
                arrival_to_fill_bps = sign * (
                    fill_price / arrival_price - 1.0
                ) * 10_000

            details.append({
                "order_id": order_id,
                "valid_date": order.valid_date,
                "symbol": order.symbol,
                "direction": order.direction,
                "status": order.status,
                "strategy_reference_price": strategy_price,
                "strategy_price_source": "raw_signals.reference_price",
                "arrival_reference_price": arrival_price,
                "limit_price": float(order.limit_price),
                "fill_vwap": fill_price,
                "fill_source": (
                    "execution_quality.fill_vwap"
                    if used_quality_fill else "trades.filled_price"
                ),
                "raw_price_difference": raw_price_difference,
                "strategy_to_arrival_bps": strategy_to_arrival_bps,
                "arrival_to_fill_bps": arrival_to_fill_bps,
                "strategy_to_fill_bps": strategy_to_fill_bps,
                "order_quantity": order.quantity,
                "instance_signal_quantity": instance_quantity,
                "aggregate_allocation_ratio": allocation_ratio,
                "filled_quantity": filled_quantity,
                "allocated_filled_quantity": allocated_fill_quantity,
                "allocated_filled_notional": allocated_fill_quantity * fill_price,
                "implementation_shortfall": implementation_shortfall,
                "estimated_fees": (
                    float(observation.estimated_fees) * allocation_ratio
                    if observation else None
                ),
                "filled_time": trade.filled_time if trade else None,
            })

        details.sort(
            key=lambda item: (item["valid_date"], item["filled_time"] or "", item["order_id"]),
            reverse=True,
        )
        n_orders = sum(status_count.values())
        price_observations = [
            item for item in details if item["strategy_to_fill_bps"] is not None
        ]
        weighted_notional = sum(
            float(item["allocated_filled_notional"]) for item in price_observations
        )
        weighted_shortfall_bps = (
            sum(
                float(item["strategy_to_fill_bps"])
                * float(item["allocated_filled_notional"])
                for item in price_observations
            ) / weighted_notional
            if weighted_notional else None
        )
        return {
            "instance_id": instance_id,
            "cutoff": cutoff,
            "summary": {
                "n_orders": n_orders,
                "by_status": dict(status_count),
                "by_direction": dict(direction_count),
                "fill_rate": len(details) / n_orders if n_orders else None,
                "filled_orders": len(details),
                "total_filled_quantity": sum(
                    float(item["allocated_filled_quantity"]) for item in details
                ),
                "total_filled_amount": sum(
                    float(item["allocated_filled_notional"]) for item in details
                ),
                "implementation_shortfall": sum(
                    float(item["implementation_shortfall"] or 0.0) for item in details
                ),
                "weighted_strategy_to_fill_bps": weighted_shortfall_bps,
                "strategy_price_coverage": (
                    len(price_observations) / len(details) if details else None
                ),
                "arrival_price_coverage": (
                    sum(item["arrival_reference_price"] is not None for item in details)
                    / len(details) if details else None
                ),
                "adverse_fill_count": sum(
                    float(item["strategy_to_fill_bps"] or 0.0) > 0
                    for item in details
                ),
                "favorable_fill_count": sum(
                    float(item["strategy_to_fill_bps"] or 0.0) < 0
                    for item in details
                ),
            },
            "count": len(details),
            "returned": min(len(details), limit),
            "items": details[:limit],
            "convention": {
                "positive_bps": "adverse",
                "negative_bps": "favorable",
                "buy": "fill above strategy reference is adverse",
                "sell": "fill below strategy reference is adverse",
            },
        }
