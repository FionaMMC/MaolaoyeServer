"""V53 适配器 — 月末双层 inv_vol 全天候调仓 (10 ETF)。

Phase M0: dry_run=true 模式，月末会 log 目标权重 + diff，但 return []
Phase M1: dry_run=false，输出真实 RawSignal[]

Task 11 实现：骨架 + 月末判断 + 资源加载 + dry_run 短路。
Task 12-15 会填实 _build_returns_matrix / weight→qty / 风控 / diff.
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
_V53_DIR = _HERE / "v53"


class V53Adapter(Strategy):
    """V53 v1.0 适配器 — Phase M0 dry_run。"""

    name = "v53"
    data_dir: ClassVar[Path | None] = _V53_DIR / "data"
    data_files: ClassVar[list[str]] = [
        "etf_close.parquet", "etf_meta.parquet", "etf_divid.parquet"]

    # class-level 缓存（首次 _load_resources 后保留）
    _cfg: ClassVar[dict | None] = None
    _etf_close_bundle: ClassVar[pd.DataFrame | None] = None
    _etf_meta: ClassVar[pd.DataFrame | None] = None
    _etf_divid: ClassVar[pd.DataFrame | None] = None

    def _load_resources(self) -> None:
        """懒加载 config + bundled data。各自独立加载。"""
        if type(self)._cfg is None:
            with (_V53_DIR / "config.yaml").open() as f:
                type(self)._cfg = yaml.safe_load(f)
        if type(self)._etf_close_bundle is None:
            df = pd.read_parquet(_V53_DIR / "data" / "etf_close.parquet")
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            type(self)._etf_close_bundle = df
        if type(self)._etf_meta is None:
            type(self)._etf_meta = pd.read_parquet(_V53_DIR / "data" / "etf_meta.parquet")
        if type(self)._etf_divid is None:
            # 分红表用于把建模价转成后复权（总回报），供波动/权重估计。
            # 本地端未推送时优雅退化为空表 → 建模价 == 原始价（不复权）。
            divid_path = _V53_DIR / "data" / "etf_divid.parquet"
            if divid_path.exists():
                d = pd.read_parquet(divid_path)
                if "ex_date" in d.columns:
                    d["ex_date"] = pd.to_datetime(d["ex_date"])
                type(self)._etf_divid = d
            else:
                type(self)._etf_divid = pd.DataFrame(
                    columns=["code", "ex_date", "cash"])

    def _is_month_end(self, ctx: Context, target: pd.Timestamp) -> bool:
        """约定 B：target(执行日) 是「次月第一个交易日」吗？是 → 调仓。

        服务器侧无前向交易日历，所以不能直接判断「target 是不是当月最后交易日」。
        改用：最近一个可用收盘日（< target）与 target 是否跨月。
        真实流程里 target=trade_date 是下一个交易日（未来），其 EOD 尚未入库，
        anchor 数据只到上一个交易日 T；若 T 落在上月 → target 是新月首交易日 → 调仓。
        每月仅在第一个交易日触发一次（第二个交易日起最近收盘日已是本月 → False）。

        ctx.market 返回 trade_date 是 int YYYYMMDD，需要转 datetime 才能比 year/month。
        """
        anchor = (type(self)._cfg or {}).get("month_end_anchor_etf", "510300.SH")
        try:
            df = ctx.market(anchor, category="etfs")
        except Exception:
            return False
        if df is None or df.empty:
            return False
        # trade_date 是 int YYYYMMDD
        td = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
        prior = td[td < target]
        if prior.empty:
            return False
        last_close = prior.max()
        return (last_close.year, last_close.month) != (target.year, target.month)

    def _build_close_matrix(
        self, ctx: Context, target: pd.Timestamp,
    ) -> pd.DataFrame:
        """拼 bundle (datetime trade_date) + IngestService (int YYYYMMDD trade_date) 增量。

        Args:
            ctx: 当前调用上下文
            target: 调仓目标日期 (pd.Timestamp)

        Returns:
            close_px wide DataFrame:
              - index = datetime, ≤ target
              - columns = v53 internal keys (hs300, cyb, ...) — vendor 直接消费
              - values = close
            如果某个 ETF 完全无数据，该列将不出现。
        """
        from plugins.v53.code_map import ETF_KEYS, QMT_TO_V53_KEY, V53_KEY_TO_QMT

        # 1. bundle (long format, QMT code, datetime trade_date)
        bundle = type(self)._etf_close_bundle
        if bundle is None:
            return pd.DataFrame()
        bundle = bundle[bundle["trade_date"] <= target][["trade_date", "code", "close"]]
        bundle_end = bundle["trade_date"].max() if not bundle.empty else pd.Timestamp("1900-01-01")

        # 2. IngestService 增量 (trade_date 是 int YYYYMMDD)
        incr_pieces = []
        for qmt_code in V53_KEY_TO_QMT.values():
            try:
                df = ctx.market(qmt_code, category="etfs")
            except Exception:
                continue
            if df is None or df.empty:
                continue
            df = df.copy()
            # int YYYYMMDD → datetime
            df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
            # 只取 bundle_end 之后到 target 之间的部分（严格大于 bundle_end 避免重复）
            df = df[(df["trade_date"] > bundle_end) & (df["trade_date"] <= target)]
            if df.empty:
                continue
            df["code"] = qmt_code
            incr_pieces.append(df[["trade_date", "code", "close"]])

        combined = (
            pd.concat([bundle, *incr_pieces], ignore_index=True)
            if incr_pieces else bundle
        )
        if combined.empty:
            return pd.DataFrame()

        # 3. long → wide; 列名 QMT code → internal key
        wide = combined.pivot_table(
            index="trade_date", columns="code", values="close", aggfunc="last")
        wide = wide.sort_index()
        wide = wide.rename(columns=QMT_TO_V53_KEY)

        # 4. 只保留 v53 关心的 10 个 internal key 顺序
        keep = [k for k in ETF_KEYS if k in wide.columns]
        return wide[keep]

    def _to_total_return(self, close_px: pd.DataFrame) -> pd.DataFrame:
        """原始（不复权）close 矩阵 → 总回报（后复权）矩阵，仅供波动/权重建模。

        分红除息日 close 会出现与市场无关的账面「假跌」，抬高波动估计，导致
        inv_vol 系统性低配高分红资产（债券 511260、红利 512890 等）。这里把
        除息现金加回后 pct_change 即为总回报，假跌消失。

        注意：只影响喂给 V53Strategy 的建模价。成交/盯市走 _resolve_reference_price
        读原始 close，本函数完全不碰它们 —— 实盘成交与券商持仓仍按不复权对账。

        分红表为空/缺失 → 原样返回（优雅退化，与未接入前逐值一致）。
        """
        from plugins.v53.code_map import V53_KEY_TO_QMT

        divid = type(self)._etf_divid
        if divid is None or len(divid) == 0 or close_px.empty:
            return close_px

        out = close_px.copy()
        idx = out.index
        for key in out.columns:
            qmt = V53_KEY_TO_QMT.get(key)
            if qmt is None:
                continue
            sub = divid[divid["code"] == qmt]
            if sub.empty:
                continue
            # 除息日 → 每份现金，对齐到 close 的交易日 index（非交易日的除息日忽略）
            div_on_date = (
                sub.set_index("ex_date")["cash"]
                .groupby(level=0).sum()
                .reindex(idx)
                .fillna(0.0)
            )
            if div_on_date.abs().sum() == 0.0:
                continue
            raw = out[key]
            prev = raw.shift(1)
            factor = (raw + div_on_date) / prev
            # 缺前收（首行/停牌）或前收非正 → 该日不调整
            factor = factor.where(prev > 0, other=1.0).fillna(1.0)
            out[key] = float(raw.iloc[0]) * factor.cumprod()
        return out

    def _resolve_reference_price(
        self, ctx: Context, qmt_code: str, target: pd.Timestamp,
    ) -> float | None:
        """优先 ctx.market 最近真实 close (≤ target)，回退 bundle close."""
        # 1. ctx.market path (trade_date 是 int YYYYMMDD)
        try:
            df = ctx.market(qmt_code, category="etfs")
        except Exception:
            df = None
        if df is not None and not df.empty:
            td = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
            df2 = df.assign(_dt=td)
            df2 = df2[df2["_dt"] <= target].sort_values("_dt")
            if not df2.empty:
                price = float(df2.iloc[-1]["close"])
                if price > 0:
                    return price

        # 2. bundle fallback
        bundle = type(self)._etf_close_bundle
        if bundle is None:
            return None
        sub = bundle[(bundle["code"] == qmt_code) & (bundle["trade_date"] <= target)]
        if sub.empty:
            return None
        price = float(sub.sort_values("trade_date").iloc[-1]["close"])
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
        """权重 dict → 100 股整 数量 dict。权重 0 / 无价格 / qty<100 都 skip。

        留 cash_buffer（默认 1%）现金不投：满仓时 100 股舍入向上取整 + 手续费
        会让总成本略超 NAV，导致 server precheck 把最后一笔单按现金不足切掉
        （如黄金 518880）。投 nav×(1-buffer) 给舍入/手续费留出空间。
        """
        cash_buffer = float((type(self)._cfg or {}).get("cash_buffer", 0.01))
        investable = nav * (1.0 - cash_buffer)
        out: dict[str, int] = {}
        for qmt_code, w in weights.items():
            if w <= 0 or investable <= 0:
                continue
            price = self._resolve_reference_price(ctx, qmt_code, target)
            if price is None or price <= 0:
                continue
            lots = round(investable * w / price / 100)
            if lots > 0:
                out[qmt_code] = int(lots) * 100
        return out

    def _latest_volume(
        self, ctx: Context, qmt_code: str, target: pd.Timestamp,
    ) -> int | None:
        """ctx.market 最近一天的 volume，转成「股」（≤ target）。

        QMT/行情 volume 字段单位是「手」(1 手 = 100 股)；流动性过滤要和
        qty(股) 同单位比较，所以这里 ×100 换算成股。
        (经 amount/close 核实：volume × 100 ≈ 实际成交股数。)
        """
        try:
            df = ctx.market(qmt_code, category="etfs")
        except Exception:
            return None
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

    def _estimate_qdii_premium(
        self, ctx: Context, qmt_code: str, target: pd.Timestamp,
    ) -> float | None:
        """QDII 溢价近似 = (today_close - mean(past_20_close)) / mean(past_20_close)。

        Spec O1: 真正的 IOPV API 待 Windows 端 QMT 文档确认；目前用 20 日均值近似。
        正值=溢价，负值=折价。
        """
        try:
            df = ctx.market(qmt_code, category="etfs")
        except Exception:
            return None
        if df is None or df.empty or len(df) < 21:
            return None
        td = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
        df2 = df.assign(_dt=td)
        df2 = df2[df2["_dt"] <= target].sort_values("_dt").tail(21)
        if len(df2) < 21:
            return None
        mean20 = float(df2.iloc[:-1]["close"].mean())
        close_today = float(df2.iloc[-1]["close"])
        if mean20 <= 0:
            return None
        return (close_today - mean20) / mean20

    def _apply_risk_filters(
        self, ctx: Context, target_qty: dict[str, int], target: pd.Timestamp,
    ) -> dict[str, int]:
        """Apply 4 risk filters in order: QDII premium, liquidity, max_single_etf_weight, blacklist."""
        from plugins.v53.code_map import QDII_QMT_CODES

        cfg = self._cfg or {}
        rf = cfg.get("risk_filters", {})
        out = dict(target_qty)

        # (a) QDII 溢价过滤
        threshold = rf.get("qdii_premium_threshold")
        if threshold is not None and threshold > 0:
            for code in list(out.keys()):
                if code not in QDII_QMT_CODES:
                    continue
                premium = self._estimate_qdii_premium(ctx, code, target)
                if premium is not None and premium > threshold:
                    logger.warning(
                        "V53 QDII %s 溢价 %.2f%% > %.2f%%, skip",
                        code, premium * 100, threshold * 100,
                    )
                    out.pop(code)

        # (b) 流动性过滤
        liq_mul = rf.get("liquidity_multiplier")
        if liq_mul is not None and liq_mul > 0:
            for code, qty in list(out.items()):
                vol = self._latest_volume(ctx, code, target)
                if vol is not None and vol < qty * liq_mul:
                    logger.warning(
                        "V53 %s 流动性不足: vol=%d < %dx%d=%d, skip",
                        code, vol, liq_mul, qty, qty * liq_mul,
                    )
                    out.pop(code)

        # (c) max_single_etf_weight cap
        max_w = rf.get("max_single_etf_weight")
        if max_w is not None and 0 < max_w < 1.0:
            nav = self._compute_nav(ctx, target)
            if nav > 0:
                for code, qty in list(out.items()):
                    price = self._resolve_reference_price(ctx, code, target)
                    if price is None or price <= 0:
                        continue
                    w = qty * price / nav
                    if w > max_w:
                        capped_qty = int(max_w * nav / price / 100) * 100
                        logger.warning(
                            "V53 %s 单 ETF 权重 %.2f > %.2f, cap qty %d → %d",
                            code, w, max_w, qty, capped_qty,
                        )
                        out[code] = capped_qty

        # (d) blacklist
        try:
            bl = ctx.risk_blacklist()
        except Exception:
            bl = set()
        for code in list(out.keys()):
            if code in bl:
                logger.info("V53 %s 在 blacklist, skip", code)
                out.pop(code)

        return out

    def _diff_and_emit(
        self,
        ctx: Context,
        current: dict[str, int],
        target: dict[str, int],
        target_ts: pd.Timestamp,
    ) -> list[RawSignal]:
        """对比 current vs target positions → 产生 RawSignal[]，SELL 先 BUY 后。

        Args:
            ctx: 当前调用上下文
            current: 当前 QMT 持仓 {symbol: qty}
            target: 调仓后目标 {symbol: qty}
            target_ts: 调仓日 timestamp (用于查 reference price)
        """
        signals: list[RawSignal] = []
        all_codes = set(current) | set(target)

        # SELL first
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
                    # SELL offset 必须为 0：QMT 对卖单按偏移后的限价校验会废单。
                    reference_price=price, price_offset=0.0,
                ))

        # BUY second
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
        """V53 完整调仓 pipeline:

        1. 加载 config + bundle
        2. 月末判断（非月末 return []）
        3. 拼 close_px (bundle + IngestService 增量)
        4. V53Strategy.compute_targets(close_px, target) → {QMT_code: weight}
        5. NAV → weight → quantity (100 股整)
        6. 风控过滤（QDII / 流动性 / max_w / blacklist）
        7. diff vs ctx.positions() → RawSignal[]
        8. dry_run=true → log + return []；dry_run=false → 返回真实 signals
        """
        from plugins.v53.strategy import V53Strategy

        target = pd.to_datetime(str(trade_date), format="%Y%m%d")

        # 1. 加载资源
        try:
            self._load_resources()
        except Exception as e:
            logger.warning("V53 资源加载失败: %s", e)
            return []

        # 2. 月末判断
        if not self._is_month_end(ctx, target):
            return []

        # 3. 拼 close_px
        close_px = self._build_close_matrix(ctx, target)
        cfg = self._cfg or {}
        min_hist = int(cfg.get("min_history_days", 126))
        if close_px.empty or len(close_px) < min_hist:
            logger.warning(
                "V53 close_px 不足 %d < %d 行, skip rebal",
                len(close_px), min_hist,
            )
            return []

        # 4. 调 V53Strategy（建模用后复权总回报价，消除分红假跌对波动的污染；
        #    执行/盯市仍用原始 close，见 _resolve_reference_price）
        close_px_model = self._to_total_return(close_px)
        strat = V53Strategy(
            quadrants=cfg.get("quadrants"),
            method=cfg.get("algorithm", "inv_vol"),
        )
        try:
            weights = strat.compute_targets(close_px_model, target)
        except Exception as e:
            logger.exception("V53 compute_targets 失败: %s", e)
            return []

        if not weights:
            logger.warning("V53 weights empty, skip rebal")
            return []

        # 5. NAV → quantity
        nav = self._compute_nav(ctx, target)
        target_qty = self._weights_to_quantities(weights, nav, ctx, target)

        # 6. 风控
        target_qty = self._apply_risk_filters(ctx, target_qty, target)

        # 7. diff
        current_positions = ctx.positions()
        signals = self._diff_and_emit(ctx, current_positions, target_qty, target)

        # 8. dry_run 处理
        if cfg.get("dry_run", True):
            logger.info(
                "V53[%s] DRY-RUN trade_date=%s nav=%.2f target_qty=%s would_emit=%d signals",
                ctx.instance_id, trade_date, nav, target_qty, len(signals),
            )
            return []

        logger.info(
            "V53[%s] go-live trade_date=%s nav=%.2f emitted=%d signals",
            ctx.instance_id, trade_date, nav, len(signals),
        )
        return signals
