"""告警引擎：跑检查套件 → severity 标注的 Alert 列表。Sink 抽象预留微信推送。"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Alert:
    id: str
    severity: str        # info | warn | critical
    category: str
    message: str
    as_of: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "severity": self.severity, "category": self.category,
                "message": self.message, "as_of": self.as_of, "detail": self.detail}


class AlertSink(Protocol):
    def emit(self, alerts: list[Alert]) -> None: ...


class DashboardSink:
    """页面 sink：仅保留最近一次结果供 /admin/alerts 返回。"""
    def __init__(self):
        self._latest: list[Alert] = []
    def emit(self, alerts: list[Alert]) -> None:
        self._latest = list(alerts)
    def latest(self) -> list[Alert]:
        return self._latest


class AlertEngine:
    def __init__(self, ops, instances: list[str], sinks: list[AlertSink] | None = None,
                 overnight_threshold: float = 0.5, market_lag_warn: int = 3,
                 pending_max_age_days: int = 2, orphan_lookback_days: int = 7):
        self.ops = ops
        self.instances = instances
        self.sinks = sinks or []
        self.overnight_threshold = overnight_threshold
        self.market_lag_warn = market_lag_warn
        self.pending_max_age_days = pending_max_age_days
        self.orphan_lookback_days = orphan_lookback_days

    def run_checks(self, today: str | None = None) -> list[Alert]:
        alerts: list[Alert] = []
        for inst in self.instances:
            for a in self.ops.overnight_position_anomalies(inst, self.overnight_threshold):
                alerts.append(Alert(
                    id=f"posanom:{inst}:{a['symbol']}:{a['to_date']}",
                    severity="critical", category="position_anomaly",
                    message=f"{inst} {a['symbol']} 隔夜 {a['prev_qty']:.0f}→{a['cur_qty']:.0f}（×{a['ratio']}）",
                    as_of=a["to_date"], detail=a))
            for iss in self.ops.snapshot_integrity(inst)["issues"]:
                alerts.append(Alert(
                    id=f"frozen:{inst}:{iss['date']}", severity="warn",
                    category="snapshot_integrity",
                    message=f"{inst} NAV 快照疑似冻结 @ {iss['date']}",
                    as_of=iss["date"], detail=iss))
        if self.ops.store is not None:
            fr = self.ops.data_freshness(today=today)
            if fr["market_lag_days"] is not None and fr["market_lag_days"] > self.market_lag_warn:
                alerts.append(Alert(
                    id=f"stale_market:{fr['market_latest']}", severity="warn",
                    category="data_freshness",
                    message=f"行情陈旧 {fr['market_lag_days']} 天（latest {fr['market_latest']}）",
                    as_of=str(fr["market_latest"]), detail=fr))
        runs = self.ops.pipeline_runs(lookback_days=5, today=today)
        missing = [r for r in runs if r["status"] == "missing"]
        if missing:
            last = missing[-1]
            alerts.append(Alert(
                id=f"pipeline_missing:{last['valid_date']}", severity="critical",
                category="pipeline",
                message=f"管线缺失运行：{', '.join(r['valid_date'] for r in missing)}",
                as_of=last["valid_date"], detail={"missing": [r["valid_date"] for r in missing]}))
        stale = self.ops.stale_pending_orders(self.pending_max_age_days, today=today)
        if stale:
            earliest = stale[0]
            alerts.append(Alert(
                id=f"stale_pending:{earliest['valid_date']}:{len(stale)}", severity="warn",
                category="stale_pending",
                message=f"{len(stale)} 笔挂单僵在 PENDING（最早 {earliest['valid_date']}，已 {earliest['age_days']} 天）—— 成交回报 order_id 对不上，卖单收不到 fill",
                as_of=earliest["valid_date"],
                detail={"count": len(stale), "orders": stale[:20]}))
        orphans = self.ops.orphan_fills(self.orphan_lookback_days, today=today)
        if orphans:
            last_orph = orphans[-1]
            alerts.append(Alert(
                id=f"orphan_fills:{last_orph['received_at'][:10]}:{len(orphans)}", severity="warn",
                category="orphan_fills",
                message=f"近 {self.orphan_lookback_days} 天 {len(orphans)} 笔成交 order_id 对不上任何订单（孤儿成交，client↔server 闭环断裂）",
                as_of=last_orph["received_at"],
                detail={"count": len(orphans), "sample": orphans[:20]}))
        for s in self.sinks:
            s.emit(alerts)
        return alerts
