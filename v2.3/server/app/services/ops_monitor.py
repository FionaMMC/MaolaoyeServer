"""运营监控只读逻辑：从 perf_snapshots/raw_signals/orders + parquet 推断健康度。"""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta
from sqlalchemy import select, desc
from app.models import PerfSnapshot, RawSignal, Order, InstanceState


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
                norders = s.execute(select(func.count()).select_from(Order)
                                    .where(Order.valid_date == vd)).scalar() or 0
                status = "weekend" if not weekday else ("ok" if sig else "missing")
                out.append({"valid_date": vd, "weekday": weekday, "status": status,
                            "signal_time": sig[0] if sig else None, "orders": int(norders)})
        return out

    def data_freshness(self, today: str | None = None,
                       probe: tuple[str, str] = ("indexes", "000852.SH")) -> dict:
        t = _d(today) if today else datetime.now().date()
        latest = self.store.latest_date(*probe) if self.store else None
        lag = (t - _d(str(latest))).days if latest else None
        return {"market_latest": latest, "market_lag_days": lag, "probe": f"{probe[0]}/{probe[1]}"}
