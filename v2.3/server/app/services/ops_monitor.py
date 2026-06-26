"""运营监控只读逻辑：从 perf_snapshots/raw_signals/orders + parquet 推断健康度。"""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta
from sqlalchemy import select, desc
from app.models import PerfSnapshot, RawSignal, Order, InstanceState, Trade


def _d(yyyymmdd: str) -> date:
    s = str(yyyymmdd); return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


class OpsMonitorService:
    def __init__(self, session_factory, parquet_store=None, settings=None):
        self.sf = session_factory
        self.store = parquet_store
        self.settings = settings

    def _snaps(self, session, instance_id, lookback):
        rows = session.execute(
            select(PerfSnapshot.date, PerfSnapshot.nav, PerfSnapshot.daily_return,
                   PerfSnapshot.positions_snapshot)
            .where(PerfSnapshot.instance_id == instance_id)
            .order_by(desc(PerfSnapshot.date)).limit(lookback)
        ).all()
        return list(reversed(rows))  # 升序

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
            p = float(prev.get(sym, 0)); c = float(cur.get(sym, 0))
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
