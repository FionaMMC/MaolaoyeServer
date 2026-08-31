"""运营监控只读逻辑：从 perf_snapshots/raw_signals/orders + parquet 推断健康度。"""
from __future__ import annotations
import json
import math
import statistics
from collections import Counter
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select, desc
from app.models import (
    ExecutionQualityObservation,
    InstanceState,
    Order,
    OrderSignalMap,
    PerfSnapshot,
    RawSignal,
    ShadowInstanceState,
    ShadowNavSnapshot,
    Trade,
)


def _d(yyyymmdd: str) -> date:
    s = str(yyyymmdd)
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _age_seconds(value: str | None, now: datetime) -> int | None:
    """Best-effort ISO timestamp age; never turns malformed operational data into a 500."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        return max(0, int((now - parsed.astimezone(now.tzinfo)).total_seconds()))
    except (TypeError, ValueError):
        return None


def _market_phase(now_cn: datetime) -> dict:
    """China A-share clock phase (not a holiday calendar)."""
    if now_cn.weekday() >= 5:
        phase = "closed"
    else:
        current = now_cn.time()
        if current < time(9, 15):
            phase = "pre_market"
        elif current < time(9, 30):
            phase = "auction"
        elif current <= time(11, 30):
            phase = "continuous"
        elif current < time(13, 0):
            phase = "lunch_break"
        elif current <= time(15, 0):
            phase = "continuous"
        elif current <= time(15, 30):
            phase = "post_close"
        else:
            phase = "closed"
    return {
        "phase": phase,
        "exchange_time": now_cn.isoformat(timespec="seconds"),
        "calendar_aware": False,
    }


class OpsMonitorService:
    def __init__(self, session_factory, parquet_store=None, settings=None):
        self.sf = session_factory
        self.store = parquet_store
        self.settings = settings

    def _snaps(self, session, instance_id, lookback):
        if instance_id.startswith("Shadow_"):
            stmt = (
                select(
                    ShadowNavSnapshot.date,
                    ShadowNavSnapshot.nav,
                    ShadowNavSnapshot.daily_return,
                    ShadowNavSnapshot.positions_snapshot,
                )
                .where(ShadowNavSnapshot.shadow_id == instance_id)
                .order_by(desc(ShadowNavSnapshot.date)).limit(lookback)
            )
        else:
            stmt = (
                select(
                    PerfSnapshot.date,
                    PerfSnapshot.nav,
                    PerfSnapshot.daily_return,
                    PerfSnapshot.positions_snapshot,
                )
                .where(PerfSnapshot.instance_id == instance_id)
                .order_by(desc(PerfSnapshot.date)).limit(lookback)
            )
        rows = session.execute(stmt).all()
        return list(reversed(rows))  # 升序

    def _orders_for_instance(self, session, instance_id: str, cutoff: str) -> tuple[list, str]:
        """Resolve instance orders without silently mixing unrelated account groups."""
        mapped_ids = session.execute(
            select(OrderSignalMap.order_id)
            .join(RawSignal, RawSignal.signal_id == OrderSignalMap.signal_id)
            .where(RawSignal.instance_id == instance_id, RawSignal.valid_date >= cutoff)
        ).scalars().all()
        if mapped_ids:
            rows = session.execute(
                select(Order)
                .where(Order.order_id.in_(mapped_ids), Order.valid_date >= cutoff)
                .order_by(desc(Order.created_at))
            ).scalars().all()
            return rows, "signal_map"

        groups = session.execute(select(Order.account_group).distinct()).scalars().all()
        candidates = [g for g in groups if instance_id == g or instance_id.startswith(f"{g}_")]
        if not candidates:
            return [], "unmapped"
        group = max(candidates, key=len)
        rows = session.execute(
            select(Order)
            .where(Order.account_group == group, Order.valid_date >= cutoff)
            .order_by(desc(Order.created_at))
        ).scalars().all()
        return rows, f"account_group:{group}"

    def live_snapshot(self, instance_id: str, lookback_days: int = 30) -> dict:
        """One-call, read-only snapshot for the 24h live command center.

        The response distinguishes observed metrics from missing telemetry. This is
        deliberate: a live dashboard must never render invented latency, exposure,
        or broker-connectivity values as healthy zeros.
        """
        now = datetime.now().astimezone()
        now_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
        cutoff = (now_cn.date() - timedelta(days=lookback_days)).strftime("%Y%m%d")

        with self.sf() as session:
            state = session.get(InstanceState, instance_id)
            shadow = None if state else session.get(ShadowInstanceState, instance_id)
            is_shadow = shadow is not None

            if state:
                snap_rows = session.execute(
                    select(PerfSnapshot)
                    .where(PerfSnapshot.instance_id == instance_id)
                    .order_by(desc(PerfSnapshot.date)).limit(252)
                ).scalars().all()
                cash = float(state.virtual_cash)
                positions = state.virtual_positions or {}
                last_update = state.last_update
            elif shadow:
                snap_rows = session.execute(
                    select(ShadowNavSnapshot)
                    .where(ShadowNavSnapshot.shadow_id == instance_id)
                    .order_by(desc(ShadowNavSnapshot.date)).limit(252)
                ).scalars().all()
                cash = float(shadow.virtual_cash)
                positions = shadow.virtual_positions or {}
                last_update = shadow.last_update
            else:
                snap_rows, cash, positions, last_update = [], None, {}, None

            orders, order_scope = ([], "shadow_no_orders") if is_shadow else self._orders_for_instance(
                session, instance_id, cutoff,
            )
            order_ids = [o.order_id for o in orders]
            trades = []
            quality = []
            for offset in range(0, len(order_ids), 500):
                chunk = order_ids[offset:offset + 500]
                trades.extend(session.execute(
                    select(Trade).where(Trade.order_id.in_(chunk))
                ).scalars().all())
                quality.extend(session.execute(
                    select(ExecutionQualityObservation)
                    .where(ExecutionQualityObservation.order_id.in_(chunk))
                ).scalars().all())
            trades.sort(key=lambda row: row.id, reverse=True)

        latest = snap_rows[0] if snap_rows else None
        previous = snap_rows[1] if len(snap_rows) > 1 else None
        nav = float(latest.nav) if latest else None
        navs = [float(row.nav) for row in snap_rows]
        returns = [float(row.daily_return) for row in snap_rows
                   if row.daily_return is not None]
        returns20 = returns[:20]
        peak = max(navs) if navs else None
        current_drawdown = (nav / peak - 1) if nav is not None and peak else None
        volatility20 = (
            statistics.stdev(returns20) * math.sqrt(252) if len(returns20) >= 2 else None
        )
        var95 = expected_shortfall95 = None
        if len(returns) >= 20:
            ordered = sorted(returns)
            idx = max(0, math.ceil(0.05 * len(ordered)) - 1)
            var95 = ordered[idx]
            tail = [item for item in ordered if item <= var95]
            expected_shortfall95 = sum(tail) / len(tail) if tail else None

        status_counts = Counter(order.status for order in orders)
        filled_orders = status_counts["FILLED"] + status_counts["PARTIAL"]
        fill_rate = filled_orders / len(orders) if orders else None
        reject_rate = status_counts["REJECTED"] / len(orders) if orders else None
        submitted_notional = sum(float(o.quantity) * float(o.limit_price) for o in orders)
        pending_notional = sum(
            float(o.quantity) * float(o.limit_price) for o in orders if o.status == "PENDING"
        )
        filled_notional = sum(float(t.filled_quantity) * float(t.filled_price) for t in trades)

        def weighted(field: str) -> float | None:
            pairs = []
            for row in quality:
                value = getattr(row, field)
                if value is None:
                    continue
                weight = float(row.filled_quantity) * float(row.fill_vwap or 0)
                pairs.append((float(value), weight if weight > 0 else 1.0))
            total_weight = sum(item[1] for item in pairs)
            return sum(value * weight for value, weight in pairs) / total_weight if total_weight else None

        offsets = [
            abs((float(o.limit_price) / float(o.execution_reference_price) - 1) * 10_000)
            for o in orders if o.execution_reference_price and o.limit_price
        ]
        max_offset = max(offsets) if offsets else None
        configured_limit = float(getattr(self.settings, "live_max_price_offset_bps", 0) or 0)
        price_protection_limit = configured_limit if configured_limit > 0 else 50.0
        shortfalls = [abs(float(q.execution_shortfall_bps)) for q in quality
                      if q.execution_shortfall_bps is not None]
        latest_fill_at = next(
            (t.received_at or t.filled_time for t in trades if t.received_at or t.filled_time), None,
        )
        stale_pending = [
            o for o in orders
            if o.status == "PENDING" and o.valid_date <= (
                now_cn.date() - timedelta(days=2)
            ).strftime("%Y%m%d")
        ]
        divergences = [o for o in orders if o.bookkeeping_divergence]
        integrity = self.snapshot_integrity(instance_id, min(lookback_days, 30)) if snap_rows else {
            "issues": [], "checked": 0,
        }
        anomalies = self.overnight_position_anomalies(instance_id) if snap_rows else []

        sorted_positions = sorted(
            ({"symbol": str(symbol), "quantity": float(quantity)}
             for symbol, quantity in positions.items()),
            key=lambda item: abs(item["quantity"]), reverse=True,
        )
        latest_return = float(latest.daily_return) if latest and latest.daily_return is not None else None
        day_pnl = (nav - float(previous.nav)) if nav is not None and previous else None

        coverage = [
            {"key": "broker_connection", "label": "QMT 连接心跳", "priority": "P0",
             "status": "missing", "next": "client 每 5s 上报连接状态与最后成功查询时间"},
            {"key": "market_tick_age", "label": "逐笔行情延迟", "priority": "P0",
             "status": "missing", "next": "上报 tick exchange_ts / receive_ts，计算 p50/p95/p99"},
            {"key": "order_ack_latency", "label": "委托确认延迟", "priority": "P0",
             "status": "missing", "next": "保存 submit_ts / broker_ack_ts / first_fill_ts"},
            {"key": "marked_exposure", "label": "盯市总/净暴露", "priority": "P0",
             "status": "missing", "next": "持仓快照增加 raw mark price 与 market value"},
            {"key": "sector_liquidity", "label": "行业集中度与流动性", "priority": "P1",
             "status": "missing", "next": "接入申万行业、ADV20、spread、participation rate"},
            {"key": "risk_contribution", "label": "风险贡献 / 压力测试", "priority": "P1",
             "status": "missing", "next": "固化协方差、MRC/RC、情景冲击到 risk_snapshot"},
        ]

        return {
            "as_of": now.isoformat(timespec="seconds"),
            "market": _market_phase(now_cn),
            "instance": {
                "instance_id": instance_id,
                "found": bool(state or shadow),
                "kind": "shadow" if is_shadow else "regular",
                "orders_enabled": not is_shadow,
                "last_state_update": last_update,
                "state_age_seconds": _age_seconds(last_update, now),
                "nav_date": latest.date if latest else None,
                "nav": nav,
                "cash": cash,
                "cash_ratio": cash / nav if cash is not None and nav else None,
                "holdings_count": len(positions),
            },
            "risk": {
                "daily_return": latest_return,
                "daily_pnl": day_pnl,
                "current_drawdown": current_drawdown,
                "rolling_volatility_20d": volatility20,
                "historical_var_95_1d": var95,
                "expected_shortfall_95_1d": expected_shortfall95,
                "sample_days": len(returns),
            },
            "execution": {
                "window_days": lookback_days,
                "scope": order_scope,
                "orders_total": len(orders),
                "status_counts": dict(status_counts),
                "fill_rate": fill_rate,
                "reject_rate": reject_rate,
                "submitted_notional": submitted_notional,
                "pending_notional": pending_notional,
                "filled_notional": filled_notional,
                "weighted_shortfall_bps": weighted("execution_shortfall_bps"),
                "weighted_premium_bps": weighted("premium_bps"),
                "estimated_fees": sum(float(q.estimated_fees) for q in quality),
                "max_abs_shortfall_bps": max(shortfalls) if shortfalls else None,
                "last_fill_at": latest_fill_at,
                "last_fill_age_seconds": _age_seconds(latest_fill_at, now),
            },
            "controls": {
                "price_protection_limit_bps": price_protection_limit,
                "max_price_offset_bps_observed": max_offset,
                "price_protection_utilization": (
                    max_offset / price_protection_limit if max_offset is not None else None
                ),
                "bookkeeping_divergences": len(divergences),
                "stale_pending_orders": len(stale_pending),
                "snapshot_integrity_issues": len(integrity.get("issues", [])),
                "overnight_position_anomalies": len(anomalies),
            },
            "positions": sorted_positions,
            "recent_orders": [{
                "order_id": o.order_id,
                "valid_date": o.valid_date,
                "created_at": o.created_at,
                "symbol": o.symbol,
                "direction": o.direction,
                "quantity": o.quantity,
                "limit_price": o.limit_price,
                "status": o.status,
            } for o in orders[:12]],
            "freshness": self.data_freshness(),
            "coverage_gaps": coverage,
        }

    def snapshot_integrity(self, instance_id: str, lookback: int = 30) -> dict:
        """检测冻结(连续相同 nav 且交易日 ret≈0)。"""
        with self.sf() as s:
            rows = self._snaps(s, instance_id, lookback)
        issues = []
        for i in range(1, len(rows)):
            d, nav, ret, _ = rows[i]
            pd_, pnav, _, _ = rows[i - 1]
            wd = _d(d).isoweekday() <= 5
            if wd and (ret == 0.0 or ret is None) and pnav is not None and nav == pnav:
                issues.append({"type": "frozen", "date": d, "nav": nav,
                               "detail": f"nav identical to {pd_}, daily_return=0 on trading day"})
        return {"instance_id": instance_id, "checked": len(rows), "issues": issues}

    def overnight_position_anomalies(self, instance_id: str, threshold: float = 0.5) -> list[dict]:
        """比较最近两份 positions_snapshot，|Δqty|/prev 超阈值的单标的（含新增/清零）。"""
        with self.sf() as s:
            rows = self._snaps(s, instance_id, lookback=2)
        if len(rows) < 2:
            return []
        raw_prev = rows[0][3]
        raw_cur = rows[1][3]
        prev = json.loads(raw_prev) if isinstance(raw_prev, str) else (raw_prev or {})
        cur = json.loads(raw_cur) if isinstance(raw_cur, str) else (raw_cur or {})
        out = []
        for sym in sorted(set(prev) | set(cur)):
            p = float(prev.get(sym, 0))
            c = float(cur.get(sym, 0))
            if p == 0 and c == 0:
                continue
            ratio = (c / p) if p else float("inf")
            change = abs(c - p) / p if p else float("inf")
            if change > threshold:
                out.append({"symbol": sym, "prev_qty": p, "cur_qty": c,
                            "ratio": round(ratio, 4) if p else None,
                            "from_date": rows[0][0], "to_date": rows[1][0]})
        return out

    def pipeline_runs(self, lookback_days: int = 14, today: str | None = None) -> list[dict]:
        """逐交易日推断管线是否跑过。

        关键：run-detection 以 perf_snapshot（每个交易日收盘标 NAV 的心跳）为准，
        而非 raw_signals。周更策略只在调仓日发信号，非调仓交易日零信号是常态——
        但只要当天写了快照，管线就确实跑过。于是三态：
          ok        有信号（跑了且调仓）
          no_signal 无信号但有快照（跑了，当天没调仓）—— 不是缺失，不该告警
          missing   无信号且无快照（管线真没跑）—— 唯一触发 critical 的状态
        """
        from sqlalchemy import func
        end = _d(today) if today else datetime.now().date()
        days = [(end - timedelta(days=k)) for k in range(lookback_days + 1)]
        out = []
        with self.sf() as s:
            for dd in sorted(days):
                vd = dd.strftime("%Y%m%d")
                weekday = dd.isoweekday() <= 5
                sig = s.execute(select(RawSignal.signal_time)
                                .where(RawSignal.valid_date == vd).limit(1)).first()
                snap = s.execute(select(PerfSnapshot.date)
                                 .where(PerfSnapshot.date == vd).limit(1)).first()
                norders = s.execute(select(func.count()).select_from(Order)
                                    .where(Order.valid_date == vd)).scalar() or 0
                if not weekday:
                    status = "weekend"
                elif sig:
                    status = "ok"
                elif snap:
                    status = "no_signal"
                else:
                    status = "missing"
                out.append({"valid_date": vd, "weekday": weekday, "status": status,
                            "snapshot": bool(snap),
                            "signal_time": sig[0] if sig else None, "orders": int(norders)})
        return out

    def data_freshness(self, today: str | None = None,
                       probe: tuple[str, str] = ("indexes", "000852.SH")) -> dict:
        t = _d(today) if today else datetime.now().date()
        latest = self.store.latest_date(*probe) if self.store else None
        lag = (t - _d(str(latest))).days if latest else None
        return {"market_latest": latest, "market_lag_days": lag, "probe": f"{probe[0]}/{probe[1]}"}

    def stale_pending_orders(self, max_age_days: int = 2,
                             today: str | None = None) -> list[dict]:
        """status=PENDING 且 valid_date 早于 today-max_age_days 的僵尸挂单（升序）。

        模拟盘成交回报 order_id 对不上时，server 真实下的卖单拿不到匹配 fill，
        永远停在 PENDING：既污染订单/告警视图，又意味着 vol_target 减仓没在虚拟
        账本兑现。背靠 valid_date 而非 created_at——挂单过了竞价日就不可能再成交。
        """
        end = _d(today) if today else datetime.now().date()
        cutoff = (end - timedelta(days=max_age_days)).strftime("%Y%m%d")
        with self.sf() as s:
            rows = s.execute(
                select(Order.order_id, Order.account_group, Order.symbol,
                       Order.direction, Order.quantity, Order.valid_date)
                .where(Order.status == "PENDING", Order.valid_date <= cutoff)
                .order_by(Order.valid_date)
            ).all()
        return [{"order_id": oid, "account_group": ag, "symbol": sym,
                 "direction": direction, "quantity": qty, "valid_date": vd,
                 "age_days": (end - _d(vd)).days}
                for oid, ag, sym, direction, qty, vd in rows]

    def orphan_fills(self, lookback_days: int = 7,
                     today: str | None = None) -> list[dict]:
        """近 lookback_days 天 order_id 在 orders 表无父订单的成交（孤儿成交）。

        client↔server 的 order_id 闭环断裂的直接证据。限定时间窗，避免历史存量
        孤儿（模拟盘累计上千笔）长期淹没告警，只反映"当下闭环是否还在断"。
        """
        end = _d(today) if today else datetime.now().date()
        cutoff = (end - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        with self.sf() as s:
            order_ids = {x[0] for x in s.execute(select(Order.order_id)).all()}
            rows = s.execute(
                select(Trade.order_id, Trade.filled_quantity,
                       Trade.filled_price, Trade.received_at)
                .where(Trade.received_at >= cutoff)
                .order_by(Trade.received_at)
            ).all()
        return [{"order_id": oid, "filled_quantity": q, "filled_price": px,
                 "received_at": ra}
                for oid, q, px, ra in rows if oid not in order_ids]

    def bookkeeping_divergences(self) -> list[dict]:
        with self.sf() as s:
            rows = s.execute(
                select(Order.order_id, Order.account_group, Order.symbol, Order.valid_date)
                .where(Order.bookkeeping_divergence.is_(True))
                .order_by(Order.valid_date.desc())
            ).all()
        return [{"order_id": oid, "account_group": group, "symbol": symbol,
                 "valid_date": valid_date}
                for oid, group, symbol, valid_date in rows]

    def blocked_shadows(self) -> list[dict]:
        with self.sf() as s:
            rows = s.execute(
                select(
                    ShadowInstanceState.shadow_id,
                    ShadowInstanceState.state_reason,
                    ShadowInstanceState.last_update,
                ).where(ShadowInstanceState.status == "blocked")
            ).all()
        return [{"shadow_id": shadow_id, "reason": reason, "last_update": last_update}
                for shadow_id, reason, last_update in rows]
