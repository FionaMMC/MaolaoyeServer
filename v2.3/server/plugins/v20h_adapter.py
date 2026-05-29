"""V20H 策略 adapter：把 plugins/v20h/ 的 V20HStrategy 接到 v2.3 server 框架。

Phase 14a: dry-run mode — 日志输出今日决策，不发 RawSignal（永远返回空 list）。
Phase 14c: 实盘 mode — 输出真实 RawSignal[]，期货部分仍 skip 直到 v2.4。

当前版本：Phase 14c（实盘）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import yaml

from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context

# vendored 策略代码
from plugins.v20h.strategy import StrategyConfig, V20HStrategy, compute_expanding_quantiles

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_V20H_DIR = _HERE / "v20h"


# ── code 格式转换 ─────────────────────────────────────────────────────
def _v20h_to_qmt_code(code6: str) -> str:
    """6 位无后缀 → QMT 格式，规则: 6 开头 .SH，其他 .SZ。"""
    if code6.startswith("6") or code6.startswith("9") or code6.startswith("688"):
        return f"{code6}.SH"
    return f"{code6}.SZ"


def _qmt_to_v20h_code(qmt: str) -> str:
    return qmt.split(".")[0]


# ── 资源缓存：路径 → (mtime, value)，按文件 mtime 自动失效（P0-2 修复）─────────
# 旧实现把 pred/v12/index 加载进类级缓存且永不失效 → 长驻 uvicorn 进程在每周
# pred 刷新上传后仍用旧 pred 直到重启（静默吞掉新数据）。改成按 mtime 缓存后，
# data-upload 覆盖文件即触发下次 run() 重载，无需重启。
_RESOURCE_CACHE: dict[str, tuple[float, object]] = {}


def _load_cached(path: Path, loader):
    """用 loader(path) 加载资源，按文件 mtime 缓存；mtime 变化时自动重载。

    文件不存在时 path.stat() 抛 FileNotFoundError，由调用方 run() 的 try/except
    捕获并优雅退化（返回空 signals）。
    """
    key = str(path)
    mtime = path.stat().st_mtime
    hit = _RESOURCE_CACHE.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    value = loader(path)
    _RESOURCE_CACHE[key] = (mtime, value)
    return value


def _read_cfg(path: Path) -> StrategyConfig:
    with path.open() as f:
        return StrategyConfig(**yaml.safe_load(f))


def _read_pred(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df["date"] = pd.to_datetime(df["date"])
    return df


def _read_v12(path: Path) -> pd.Series:
    v12 = pd.read_parquet(path).squeeze()
    v12.index = pd.to_datetime(v12.index)
    return v12


def _read_index(path: Path) -> pd.Series:
    idx_df = pd.read_parquet(path)
    idx_df.index = pd.to_datetime(idx_df.index)
    return idx_df["close"]


class V20HAdapter(Strategy):
    """V20H v1.3 适配器 — Phase 14c 实盘。"""

    name = "v20h_v1_3"
    data_dir = _V20H_DIR / "data"
    data_files = [
        "pred_csi1000.parquet",
        "v12_exp_hs300.parquet",
        "stock_close.parquet",
        "stock_returns.parquet",
        "index_csi1000.parquet",
    ]

    # 配置缓存（懒加载）
    _cfg: StrategyConfig | None = None
    _pred_df: pd.DataFrame | None = None
    _v12_series: pd.Series | None = None
    _index_close: pd.Series | None = None

    def _load_resources(self) -> None:
        """加载 config + 外部数据；按文件 mtime 缓存，刷新后自动重载（P0-2 修复）。

        失败（如文件缺失）则抛异常，由 run() 的 try/except 捕获后返回空 signals。
        """
        cls = type(self)
        cls._cfg = _load_cached(_V20H_DIR / "config.yaml", _read_cfg)
        cls._pred_df = _load_cached(_V20H_DIR / "data" / "pred_csi1000.parquet", _read_pred)
        cls._v12_series = _load_cached(_V20H_DIR / "data" / "v12_exp_hs300.parquet", _read_v12)
        cls._index_close = _load_cached(_V20H_DIR / "data" / "index_csi1000.parquet", _read_index)

    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        """Phase 14c 实盘：输出真实 RawSignal[]，期货部分仍 skip 直到 v2.4。"""
        try:
            self._load_resources()
        except Exception as e:
            logger.warning("V20H 资源加载失败（外部数据可能未上传）: %s", e)
            return []

        cfg = self._cfg
        pred_df = self._pred_df
        v12 = self._v12_series

        target_date = pd.to_datetime(str(trade_date), format="%Y%m%d")

        # 找 ≤ target_date 的最近一条 pred；如果 target_date 本身有就用它
        # 实盘逻辑：T 收盘后才能算 pred[T]，T+1 早盘下单时只能用 pred[T] 或更早
        applicable_dates = pred_df[pred_df["date"] <= target_date]["date"]
        if applicable_dates.empty:
            logger.warning("V20H pred_csi1000 无 %s 之前的数据，跳过", trade_date)
            return []
        latest_pred_date = applicable_dates.max()
        pred_today = pred_df[pred_df["date"] == latest_pred_date]
        if latest_pred_date != target_date:
            lag_days = (target_date - latest_pred_date).days
            logger.info(
                "V20H pred staleness: trade_date=%s 用 pred[%s] (lag %d days)",
                trade_date, latest_pred_date.strftime("%Y%m%d"), lag_days,
            )

        # ── 板块权限过滤（账户不能交易的前缀，默认科创板 688/689）─────
        excluded_prefixes = tuple(getattr(cfg, "excluded_symbol_prefixes", ()) or ())
        if excluded_prefixes:
            n_before = len(pred_today)
            mask = ~pred_today["code"].astype(str).str.startswith(excluded_prefixes)
            pred_today = pred_today[mask]
            n_dropped = n_before - len(pred_today)
            if n_dropped > 0:
                logger.info(
                    "V20H excluded prefixes %s: dropped %d/%d symbols",
                    excluded_prefixes, n_dropped, n_before,
                )

        # ── 风险黑名单过滤（QMT 历史拒单的 ST/退市/未签协议 等）──────
        blacklist_qmt = ctx.risk_blacklist()
        if blacklist_qmt:
            blacklist_v20h = {_qmt_to_v20h_code(s) for s in blacklist_qmt}
            n_before = len(pred_today)
            pred_today = pred_today[~pred_today["code"].isin(blacklist_v20h)]
            n_dropped = n_before - len(pred_today)
            if n_dropped > 0:
                logger.info(
                    "V20H risk blacklist: dropped %d/%d symbols (blacklist size=%d)",
                    n_dropped, n_before, len(blacklist_qmt),
                )

        # ── 把当前 ctx.positions(QMT 格式) 转成 V20H 6 位 code ──────
        ctx_positions = {
            _qmt_to_v20h_code(qmt): qty
            for qmt, qty in ctx.positions().items()
        }

        strategy = V20HStrategy(cfg)
        strategy.cash = ctx.cash()
        strategy.positions = dict(ctx_positions)

        # ── 恢复持久化的策略状态（Bug D 修复）────────────────────────
        # 没有持久 state 时按 __init__ 默认：last_rb_idx=-rebal_freq → 当天就 rebal
        # 这是首次跑/迁移期的正确行为。
        persisted = ctx.strategy_state() or {}
        # 同日重复 trigger 幂等（R2 修复）：上次落库的 last_trade_date == 本次 →
        # equity_history/daily_rets 里已经有本 trade_date 的点。重跑必须【替换】
        # 而非【叠加】，否则同日 9:00 + 16:00 两次 trigger 会把当日点 append 两遍，
        # 污染波动率目标窗口（线上实测出现连续重复 equity 点 + 一串 0.0 daily_ret）。
        is_rerun = str(persisted.get("last_trade_date")) == str(trade_date)
        if "last_rb_idx" in persisted:
            strategy.last_rb_idx = int(persisted["last_rb_idx"])
        if "equity_history" in persisted:
            eq_hist = list(persisted["equity_history"])
            if is_rerun and len(eq_hist) > 1:
                eq_hist = eq_hist[:-1]   # 去掉上次为本 trade_date append 的 equity 点
            strategy.equity_history = eq_hist
        if "daily_rets" in persisted:
            daily_rets = list(persisted["daily_rets"])
            if is_rerun and daily_rets:
                daily_rets = daily_rets[:-1]   # 同上：去掉本日的 daily_ret
            strategy.daily_rets = daily_rets
        if "prev_hedge" in persisted:
            strategy.prev_hedge = float(persisted["prev_hedge"])

        # 当日所有股票价格（reshape ctx market 数据为 dict）
        prices_today = self._build_prices_today(ctx, pred_today)
        if not prices_today:
            logger.warning("V20H 无可交易标的（行情缺失），跳过 %s", trade_date)
            return []

        # CSI1000 当日价
        cur_idx_price = self._read_index_close(ctx, "000852.SH", trade_date)

        # V12 + Q 阈值
        v12_val = float(v12.get(target_date, 0.5))
        q_thresh = compute_expanding_quantiles(
            v12, start=cfg.start_date,
            quantiles=[cfg.q10_quantile, cfg.q20_quantile, cfg.q40_quantile],
            warmup=cfg.q_warmup_days,
        )
        q10 = float(q_thresh[cfg.q10_quantile].get(target_date, 0.30))
        q20 = float(q_thresh[cfg.q20_quantile].get(target_date, 0.30))
        q40 = float(q_thresh[cfg.q40_quantile].get(target_date, 0.30))

        # di（日期索引）：pred 的所有 date 排序后取 target_date 的位置
        all_dates = sorted(pred_df["date"].unique())
        di = next((i for i, d in enumerate(all_dates) if d == target_date), len(all_dates))

        # ── Rebal 节奏决策 ────────────────────────────────────────────────
        # 优先用持久化的 last_rb_idx。首次跑或迁移都强制触发一次 rebal：
        #   - 首次跑（无状态 + 空仓）：建仓
        #   - 迁移（有持仓但无状态）：把老持仓收敛到当下 V20H ideal，然后
        #     之后按真正的 42 天节奏走
        # 这取代了旧的 stateless 每日 rebal 行为（Bug D）。
        if "last_rb_idx" not in persisted:
            strategy.last_rb_idx = di - cfg.rebal_freq
            rebal_reason = "first_build" if not ctx_positions else "migration"
        else:
            rebal_reason = "persisted"
        will_rebal = (di - strategy.last_rb_idx) >= cfg.rebal_freq
        logger.info(
            "V20H rebal-schedule: instance=%s di=%d freq=%d positions=%d "
            "last_rb_idx=%d will_rebal=%s reason=%s",
            ctx.instance_id, di, cfg.rebal_freq, len(ctx_positions),
            strategy.last_rb_idx, will_rebal, rebal_reason,
        )

        # 调 step()
        log_entry = strategy.step(
            date=target_date,
            prices_today=prices_today,
            cur_idx_price=cur_idx_price,
            prev_idx_price=cur_idx_price,  # Phase 14a 简化
            v12_val=v12_val,
            q10=q10, q20=q20, q40=q40,
            pred_today=pred_today,
            di=di,
            is_roll_day=False,  # Phase 14a 跳过 roll 日处理
        )

        # ── 保存策略状态供下次启动 (Bug D 修复)──────────────────────────
        # 截断 equity_history / daily_rets 到 vol_lookback × 4，避免 JSON 无限增长
        keep = max(cfg.vol_lookback * 4, 100)
        ctx.set_strategy_state({
            "last_rb_idx": int(strategy.last_rb_idx),
            "equity_history": [float(x) for x in strategy.equity_history[-keep:]],
            "daily_rets": [float(x) for x in strategy.daily_rets[-keep:]],
            "prev_hedge": float(strategy.prev_hedge),
            "last_di": int(di),
            "last_trade_date": str(trade_date),
        })

        # ── diff 目标组合 vs 当前 ────────────────────────────────────
        target_positions = dict(strategy.positions)
        before = ctx_positions
        to_buy = {c: q for c, q in target_positions.items() if q > before.get(c, 0)}
        to_sell = {c: before[c] - q for c, q in target_positions.items()
                   if c in before and before[c] > q}
        to_close = {c: before[c] for c in before if c not in target_positions}

        # Phase 14c：实盘 — 输出 RawSignal[]
        # reference_price 优先用 server 最新一日真实收盘（ctx.market），回退到 pred close。
        # 原因：pred 可能 lag 多天（每周一次刷新），直接用 pred close 当限价基准会和
        # 当下市场严重偏离 → SELL 限价虚高卖不出 / BUY 限价虚低买不到。
        signals: list[RawSignal] = []
        n_fallback = 0
        n_kcb_skipped = 0       # 科创板 < 200 股被跳过的
        n_kcb_rounded_up = 0    # 科创板 SELL 100 → 200 兜底

        def _is_kcb(code6: str) -> bool:
            """科创板 (688/689 开头)。单笔委托量最低 200 股，否则废单。"""
            return code6.startswith("688") or code6.startswith("689")

        def _emit(code6: str, qty: int, direction: str) -> None:
            nonlocal n_fallback, n_kcb_skipped, n_kcb_rounded_up
            qmt = _v20h_to_qmt_code(code6)

            # ── 科创板 200 股下限保护 ──
            # 5/26-5/27 实证：100 股 SELL 全部废单，账户级规则
            # SELL: 如果持仓 >= 200 → round up qty=200；如果持仓 < 200 → 跳过
            #       注意 SELL 不能超过实际持仓，所以"round up"实际是判断
            #       「能不能凑出 ≥200 卖」
            # BUY:  如果 < 200，跳过（不应该出现这种情况）
            if _is_kcb(code6) and qty < 200:
                if direction == "SELL":
                    cur_held = ctx_positions.get(code6, 0)
                    if cur_held >= 200:
                        # 持仓够 ≥ 200，把 qty 提到 200（cap_weight 多 trim 一点）
                        qty = 200
                        n_kcb_rounded_up += 1
                    else:
                        # 持仓 < 200 直接跳过（避免 100 股废单）
                        # 这意味着 cap_weight 想 trim 但没法精细控制 → 留着不动
                        n_kcb_skipped += 1
                        logger.debug(
                            "V20H[%s] skip 科创板 SELL %s qty=%d (持仓 %d < 200)",
                            ctx.instance_id, qmt, qty, cur_held,
                        )
                        return
                else:  # BUY
                    n_kcb_skipped += 1
                    logger.debug(
                        "V20H[%s] skip 科创板 BUY %s qty=%d (< 200 起步)",
                        ctx.instance_id, qmt, qty,
                    )
                    return

            price, used_fallback = self._resolve_reference_price(
                ctx, code6, qmt, prices_today, trade_date,
            )
            if price is None or price <= 0:
                return
            if used_fallback:
                n_fallback += 1
            signals.append(RawSignal(
                symbol=qmt,
                direction=direction,
                quantity=qty,
                reference_price=price,
                price_offset=-0.005 if direction == "SELL" else +0.005,
            ))

        # 先 SELL（V20H 不要的标的中 size 减少的）
        for code6, qty in to_sell.items():
            _emit(code6, qty, "SELL")
        # close（完全清掉的）
        for code6, qty in to_close.items():
            _emit(code6, qty, "SELL")
        # 后 BUY
        for code6, qty in to_buy.items():
            _emit(code6, qty, "BUY")

        if n_kcb_skipped or n_kcb_rounded_up:
            logger.info(
                "V20H[%s] 科创板 200 股规则: skipped %d, rounded_up_to_200 %d",
                ctx.instance_id, n_kcb_skipped, n_kcb_rounded_up,
            )

        if n_fallback > 0:
            logger.warning(
                "V20H[%s] %d/%d signals fell back to pred close as reference_price "
                "(ctx.market 无数据)，限价可能偏离市价",
                ctx.instance_id, n_fallback, len(signals),
            )

        logger.info(
            "V20H[%s] go-live trade_date=%s emitted=%d (buy=%d sell=%d close=%d)",
            ctx.instance_id, trade_date,
            len(signals), len(to_buy), len(to_sell), len(to_close),
        )
        return signals

    # ── 内部：行情读取/转换 ──────────────────────────────────────────
    def _resolve_reference_price(
        self,
        ctx: Context,
        code6: str,
        qmt_code: str,
        prices_today: dict[str, float],
        trade_date: int,
    ) -> tuple[float | None, bool]:
        """决定 RawSignal.reference_price。

        优先级：
          1. ctx.market(qmt_code) 里 trade_date 之前最近一日的真实 close（server 已有最新 OHLCV）
          2. fallback: pred_today.close（stale，可能偏离市价多日）

        返回 (price, used_fallback)。
        """
        try:
            df = ctx.market(qmt_code)
            if not df.empty:
                df_sorted = df.sort_values("trade_date")
                real_close = float(df_sorted.iloc[-1]["close"])
                if real_close > 0:
                    return real_close, False
        except Exception as e:
            logger.debug(
                "V20H _resolve_reference_price: ctx.market(%s) raised %s; fallback to pred",
                qmt_code, e,
            )
        # Fallback
        pred = prices_today.get(code6)
        if pred and pred > 0:
            return float(pred), True
        return None, True

    def _build_prices_today(
        self, ctx: Context, pred_today: pd.DataFrame,
    ) -> dict[str, float]:
        """{6位code: today_close} — 直接用 pred_today.close，bundled 数据是 V20H 训练时的真值。

        ctx 参数保留是为了未来扩展（如运行时覆盖某些价格）。
        """
        return {
            row["code"]: float(row["close"])
            for _, row in pred_today.iterrows()
            if pd.notna(row["close"]) and float(row["close"]) > 0
        }

    def _read_index_close(
        self, ctx: Context, qmt_code: str, trade_date: int,
    ) -> float | None:
        """从 bundled index_csi1000.parquet 读取目标日期的 close。"""
        if self._index_close is None:
            return None
        target_date = pd.to_datetime(str(trade_date), format="%Y%m%d")
        v = self._index_close.get(target_date)
        if v is None or pd.isna(v):
            return None
        return float(v)
