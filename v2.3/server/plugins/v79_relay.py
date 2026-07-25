"""V79 relay 适配器 — thin relay: 外部 Windows 端周更 basket → diff → RawSignal。

无策略计算、无日历/月末逻辑：basket 出现新 decision_date 就处理一次，否则空转。
幂等靠 ctx.strategy_state()['last_consumed_decision_date']，防止重跑 / order_id
churn 导致同一 basket 被重复下单。

复用 v53_adapter.py 的 helper patterns：_compute_nav / _weights_to_quantities /
_resolve_reference_price / _diff_and_emit（SELL 先 offset=0.0，BUY 后 offset=+0.005）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

import pandas as pd
import yaml

from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_V79_DIR = _HERE / "v79"


class V79RelayAdapter(Strategy):
    """V79 thin-relay 适配器：basket-driven diff，无自研策略计算。"""

    name = "v79_relay"
    data_dir: ClassVar[Path | None] = _V79_DIR / "data"
    data_files: ClassVar[list[str]] = ["v79_target_latest.parquet"]

    _cfg: ClassVar[dict | None] = None

    def _load_config(self) -> None:
        if type(self)._cfg is None:
            with (_V79_DIR / "config.yaml").open(encoding="utf-8") as f:
                type(self)._cfg = yaml.safe_load(f)

    def _read_latest_basket(self) -> pd.DataFrame | None:
        """读取 data_dir 下的最新 basket parquet。缺失/空 → None。"""
        data_dir = type(self).data_dir
        if data_dir is None:
            return None
        path = Path(data_dir) / "v79_target_latest.parquet"
        if not path.exists():
            return None
        df = pd.read_parquet(path)
        if df.empty:
            return None
        return df

    def _resolve_reference_price(
        self, ctx: Context, qmt_code: str, target: pd.Timestamp,
    ) -> float | None:
        """ctx.market 最近真实 close (≤ target)。basket 已是 QMT code，无需转换。"""
        try:
            df = ctx.market(qmt_code, category="stocks")
        except Exception:
            df = None
        if df is None or df.empty:
            try:
                df = ctx.market(qmt_code, category="etfs")
            except Exception:
                df = None
        if df is None or df.empty:
            return None
        td = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
        df2 = df.assign(_dt=td)
        df2 = df2[df2["_dt"] <= target].sort_values("_dt")
        if df2.empty:
            return None
        price = float(df2.iloc[-1]["close"])
        return price if price > 0 else None

    def _compute_nav(self, ctx: Context, target: pd.Timestamp) -> float:
        """NAV = cash + Σ(qty × ref_price)。"""
        nav = float(ctx.cash())
        for code, qty in ctx.positions().items():
            price = self._resolve_reference_price(ctx, code, target)
            if price is None:
                continue
            nav += float(qty) * float(price)
        return nav

    def _weights_to_quantities(
        self, weights: dict[str, float], nav: float,
        ctx: Context, target: pd.Timestamp,
    ) -> dict[str, int]:
        """权重 dict → 100 股整数量 dict。权重<=0 / 无价格 / lots<=0 都 skip。"""
        cash_buffer = float((self._cfg or {}).get("cash_buffer", 0.01))
        investable = nav * (1.0 - cash_buffer)
        out: dict[str, int] = {}
        for qmt_code, w in weights.items():
            if w <= 0 or investable <= 0:
                continue
            price = self._resolve_reference_price(ctx, qmt_code, target)
            if price is None or price <= 0:
                logger.warning("V79 %s 无可用参考价, skip", qmt_code)
                continue
            lots = round(investable * w / price / 100)
            if lots > 0:
                out[qmt_code] = int(lots) * 100
        return out

    def _latest_volume(
        self, ctx: Context, qmt_code: str, target: pd.Timestamp,
    ) -> int | None:
        """ctx.market 最近一天 volume（手）→ 股。镜像 v53_adapter._latest_volume。"""
        try:
            df = ctx.market(qmt_code, category="stocks")
        except Exception:
            df = None
        if df is None or df.empty:
            return None
        td = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
        df2 = df.assign(_dt=td)
        df2 = df2[df2["_dt"] <= target].sort_values("_dt")
        if df2.empty:
            return None
        v = df2.iloc[-1].get("volume", 0)
        try:
            return int(v) * 100  # 手 → 股
        except (TypeError, ValueError):
            return None

    def _apply_risk_filters(
        self, ctx: Context, target_qty: dict[str, int], target: pd.Timestamp,
    ) -> dict[str, int]:
        """最小风控：blacklist + 流动性过滤（镜像 v53，去掉 QDII/max_weight——v79 无此需求）。"""
        cfg = self._cfg or {}
        rf = cfg.get("risk_filters", {})
        out = dict(target_qty)

        liq_mul = rf.get("liquidity_multiplier")
        if liq_mul is not None and liq_mul > 0:
            for code, qty in list(out.items()):
                vol = self._latest_volume(ctx, code, target)
                if vol is not None and vol < qty * liq_mul:
                    logger.warning(
                        "V79 %s 流动性不足: vol=%d < %dx%d=%d, skip",
                        code, vol, liq_mul, qty, qty * liq_mul,
                    )
                    out.pop(code)

        try:
            bl = ctx.risk_blacklist()
        except Exception:
            bl = set()
        for code in list(out.keys()):
            if code in bl:
                logger.info("V79 %s 在 blacklist, skip", code)
                out.pop(code)

        return out

    def _diff_and_emit(
        self,
        ctx: Context,
        current: dict[str, int],
        target: dict[str, int],
        target_ts: pd.Timestamp,
    ) -> list[RawSignal]:
        """current vs target → RawSignal[]，SELL 先(offset=0.0) BUY 后(offset=+0.005)。"""
        signals: list[RawSignal] = []
        all_codes = set(current) | set(target)

        for code in sorted(all_codes):
            cur = current.get(code, 0)
            tgt = target.get(code, 0)
            if cur > tgt:
                qty = cur - tgt
                price = self._resolve_reference_price(ctx, code, target_ts)
                if price is None or price <= 0:
                    continue
                signals.append(RawSignal(
                    symbol=code, direction="SELL", quantity=qty,
                    reference_price=price, price_offset=0.0,
                ))

        for code in sorted(all_codes):
            cur = current.get(code, 0)
            tgt = target.get(code, 0)
            if tgt > cur:
                qty = tgt - cur
                price = self._resolve_reference_price(ctx, code, target_ts)
                if price is None or price <= 0:
                    continue
                signals.append(RawSignal(
                    symbol=code, direction="BUY", quantity=qty,
                    reference_price=price, price_offset=+0.005,
                ))

        return signals

    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        """V79 relay pipeline:

        1. 加载 config
        2. 读取最新 basket parquet（缺失/空 → []）
        3. decision_date 幂等检查（已消费过 → []）
        4. NAV → weight → quantity
        5. 风控（blacklist + 流动性）
        6. diff vs ctx.positions() → RawSignal[]
        7. dry_run=true → log + return []（不标记 consumed）
           dry_run=false → 标记 consumed + 返回真实 signals
        """
        target = pd.to_datetime(str(trade_date), format="%Y%m%d")

        self._load_config()
        cfg = self._cfg or {}

        basket = self._read_latest_basket()
        if basket is None:
            logger.warning("V79 basket 缺失或为空, skip")
            return []

        decision_date = str(basket["decision_date"].max())

        state = ctx.strategy_state()
        if state.get("last_consumed_decision_date") == decision_date:
            logger.info(
                "V79[%s] decision_date=%s 已消费, skip", ctx.instance_id, decision_date)
            return []

        weights = dict(zip(basket["code"], basket["weight"]))

        nav = self._compute_nav(ctx, target)
        target_qty = self._weights_to_quantities(weights, nav, ctx, target)
        target_qty = self._apply_risk_filters(ctx, target_qty, target)

        current_positions = ctx.positions()
        signals = self._diff_and_emit(ctx, current_positions, target_qty, target)

        if cfg.get("dry_run", True):
            logger.info(
                "V79[%s] DRY-RUN decision_date=%s target=%d signals=%d",
                ctx.instance_id, decision_date, len(target_qty), len(signals),
            )
            return []

        new_state = dict(state)
        new_state["last_consumed_decision_date"] = decision_date
        ctx.set_strategy_state(new_state)

        logger.info(
            "V79[%s] go-live decision_date=%s nav=%.2f emitted=%d signals",
            ctx.instance_id, decision_date, nav, len(signals),
        )
        return signals
