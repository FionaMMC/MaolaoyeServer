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
        """懒加载 config + 外部数据。失败则记日志后续 run() 返回空。"""
        if self._cfg is None:
            cfg_path = _V20H_DIR / "config.yaml"
            with cfg_path.open() as f:
                cfg_dict = yaml.safe_load(f)
            type(self)._cfg = StrategyConfig(**cfg_dict)

        if self._pred_df is None:
            pred_path = _V20H_DIR / "data" / "pred_csi1000.parquet"
            df = pd.read_parquet(pred_path)
            if not pd.api.types.is_datetime64_any_dtype(df["date"]):
                df["date"] = pd.to_datetime(df["date"])
            type(self)._pred_df = df

        if self._v12_series is None:
            v12_path = _V20H_DIR / "data" / "v12_exp_hs300.parquet"
            v12 = pd.read_parquet(v12_path).squeeze()
            v12.index = pd.to_datetime(v12.index)
            type(self)._v12_series = v12

        if self._index_close is None:
            idx_path = _V20H_DIR / "data" / "index_csi1000.parquet"
            idx_df = pd.read_parquet(idx_path)
            idx_df.index = pd.to_datetime(idx_df.index)
            type(self)._index_close = idx_df["close"]

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

        # ── 重建 V20H 状态（无状态版本：从 ctx 派生）──────────────
        # 把当前 ctx.positions(QMT 格式) 转成 V20H 6 位 code
        ctx_positions = {
            _qmt_to_v20h_code(qmt): qty
            for qmt, qty in ctx.positions().items()
        }

        strategy = V20HStrategy(cfg)
        strategy.cash = ctx.cash()
        strategy.positions = dict(ctx_positions)

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

        # ── diff 目标组合 vs 当前 ────────────────────────────────────
        target_positions = dict(strategy.positions)
        before = ctx_positions
        to_buy = {c: q for c, q in target_positions.items() if q > before.get(c, 0)}
        to_sell = {c: before[c] - q for c, q in target_positions.items()
                   if c in before and before[c] > q}
        to_close = {c: before[c] for c in before if c not in target_positions}

        # Phase 14c：实盘 — 输出 RawSignal[]
        signals: list[RawSignal] = []

        # 先 SELL（卖出 V20H 不要的标的；含 close 全部）
        for code6, qty in to_sell.items():
            qmt = _v20h_to_qmt_code(code6)
            price = prices_today.get(code6)
            if price is None or price <= 0:
                continue
            signals.append(RawSignal(
                symbol=qmt,
                direction="SELL",
                quantity=qty,
                reference_price=price,
                price_offset=-0.005,
            ))

        for code6, qty in to_close.items():
            qmt = _v20h_to_qmt_code(code6)
            price = prices_today.get(code6)
            if price is None or price <= 0:
                continue
            signals.append(RawSignal(
                symbol=qmt,
                direction="SELL",
                quantity=qty,
                reference_price=price,
                price_offset=-0.005,
            ))

        # 后 BUY
        for code6, qty in to_buy.items():
            qmt = _v20h_to_qmt_code(code6)
            price = prices_today.get(code6)
            if price is None or price <= 0:
                continue
            signals.append(RawSignal(
                symbol=qmt,
                direction="BUY",
                quantity=qty,
                reference_price=price,
                price_offset=+0.005,
            ))

        logger.info(
            "V20H[%s] go-live trade_date=%s emitted=%d (buy=%d sell=%d close=%d)",
            ctx.instance_id, trade_date,
            len(signals), len(to_buy), len(to_sell), len(to_close),
        )
        return signals

    # ── 内部：行情读取/转换 ──────────────────────────────────────────
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
