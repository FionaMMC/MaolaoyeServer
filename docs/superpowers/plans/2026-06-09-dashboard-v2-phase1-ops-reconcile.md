# Dashboard v2 — Phase 1 (运营/对账 + 健康 strip + 告警) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给现有 `GET /dashboard` 加一个运营健康/对账/告警层——常驻 header 健康 strip + 新「运营与对账」tab + 告警 feed——把「管线静默停摆 / NAV 快照冻结 / 隔夜持仓翻倍」这类事故在发生当刻顶到眼前。

**Architecture:** 监控逻辑放进可单测的服务层 `OpsMonitorService` + `AlertEngine`（纯函数式、吃 session/store 出结构化结果），API 端点保持轻薄（沿用 `APIResponse[dict]` + `verify_api_key` + Depends 注入），前端在单文件 dashboard 内加共享 JS 骨架（fetch/format/刷新调度/as-of-陈旧/告警 badge）+ 一个新 tab。零构建。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy / pytest（server venv 已装；本地用 `/Users/mameican/Desktop/server/venv/bin/python` 跑 server 测试，工作目录 `v2.3/server`）；前端 vanilla JS + Chart.js CDN。

**Spec:** `docs/superpowers/specs/2026-06-09-dashboard-v2-institutional-design.md`（本计划只做阶段一）

**关键既有事实（写代码按此）：**
- 模型 `from app.models import Order, PerfSnapshot, InstanceState, RawSignal, Trade`。
  - `PerfSnapshot(instance_id, date 'YYYYMMDD' str, nav float, daily_return float|None, positions_snapshot JSON)`
  - `RawSignal(instance_id, valid_date 'YYYYMMDD' str, signal_time iso str, ...)`
  - `Order(account_group, valid_date, status, ...)`，`InstanceState(instance_id, virtual_cash, virtual_positions JSON, last_update)`
- 路由模式：`@router.get("/x", response_model=APIResponse[dict], dependencies=[Depends(verify_api_key)])`，`router=APIRouter(prefix="/admin")`（见 `app/api/admin_query.py`）。
- 依赖注入：`get_session_factory`, `get_parquet_store`, `get_settings`（`app/dependencies.py`）。
- `ParquetStore.latest_date(category, symbol)`（本会话已加）。探针默认 `("indexes","000852.SH")`，store root = `settings.parquet_root`（真 store = `…/server/data`）。
- 既有可复用端点：`/admin/health`、`/admin/heartbeat`（已从 raw_signals/trades/parquet 推断 client 步骤 + 出 alerts list）、`/admin/bookkeeping-divergence`（server↔QMT 对账分叉）。
- 测试 conftest：`tests/conftest.py` 有 `settings_for_test`/`client`；单测惯例见 `tests/unit/test_pipeline.py`（手搓 engine/sf/store + seed）。
- dashboard 单文件：`app/api/dashboard.py`，`_DASHBOARD_HTML`；JS 有 `api(path)` fetch 助手、tab 切换、`refreshAll()`、`setInterval(refreshAll, 60000)`、instance 选择 `getInstanceId()`。

**输出文件结构：**
- `app/services/ops_monitor.py` — 快照完整性 / 隔夜持仓异常 / 管线运行重建 / 数据新鲜度（纯逻辑）。
- `app/services/alerts.py` — `Alert`、`AlertSink`、`DashboardSink`、`AlertEngine.run_checks`。
- `app/api/ops.py` — 新路由：`/admin/ops/*`、`/admin/alerts`、`/admin/dashboard-meta`。
- `app/main.py` — 注册 ops 路由。
- `app/api/dashboard.py` — header strip + 运营 tab + 共享 JS 骨架。
- 测试：`tests/unit/test_ops_monitor.py`、`tests/unit/test_alerts.py`、`tests/unit/test_api_ops.py`。

运行测试统一：`cd v2.3/server && /Users/mameican/Desktop/server/venv/bin/python -m pytest <path> -q`

---

## Task 1: OpsMonitorService — 快照完整性 + 隔夜持仓异常

**Files:**
- Create: `app/services/ops_monitor.py`
- Test: `tests/unit/test_ops_monitor.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_ops_monitor.py
import json
from pathlib import Path
import pytest
from app.db import init_db, make_engine, make_session_factory
from app.models import PerfSnapshot
from app.services.ops_monitor import OpsMonitorService

@pytest.fixture
def sf(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path}/t.db"); init_db(eng)
    return make_session_factory(eng)

def _snap(s, inst, date, nav, ret, pos):
    s.add(PerfSnapshot(instance_id=inst, date=date, nav=nav, daily_return=ret,
                       positions_snapshot=json.dumps(pos)))

def test_snapshot_integrity_flags_frozen(sf):
    inst = "paper_v53_v53"
    with sf() as s:
        _snap(s, inst, "20260522", 9_951_130, 0.013, {"X": 100})
        _snap(s, inst, "20260525", 9_951_130, 0.0, {"X": 100})  # frozen: same nav, ret 0 on trading day
        s.commit()
    svc = OpsMonitorService(sf)
    issues = svc.snapshot_integrity(inst, lookback=30)["issues"]
    assert any(i["type"] == "frozen" and i["date"] == "20260525" for i in issues)

def test_overnight_position_anomaly_flags_doubling(sf):
    inst = "paper_v53_v53"
    with sf() as s:
        _snap(s, inst, "20260608", 9_888_426, -0.0036, {"511260.SH": 49500, "510300.SH": 118500})
        _snap(s, inst, "20260609", 16_608_072, 0.68, {"511260.SH": 99000, "510300.SH": 118500})
        s.commit()
    svc = OpsMonitorService(sf)
    an = svc.overnight_position_anomalies(inst, threshold=0.5)
    assert any(a["symbol"] == "511260.SH" and round(a["ratio"], 2) == 2.0 for a in an)
    assert all(a["symbol"] != "510300.SH" for a in an)  # unchanged not flagged
```

- [ ] **Step 2: 运行确认失败** — `pytest tests/unit/test_ops_monitor.py -q` → FAIL（无 OpsMonitorService）。

- [ ] **Step 3: 实现 `app/services/ops_monitor.py`（本步先到这两个方法）**

```python
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
        """检测冻结(连续相同 nav 且交易日 ret≈0)/零收益/缺口。"""
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
        prev = json.loads(rows[0][3] or "{}")
        cur = json.loads(rows[1][3] or "{}")
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
```

- [ ] **Step 4: 运行确认通过** — `pytest tests/unit/test_ops_monitor.py -q` → PASS。

- [ ] **Step 5: Commit**

```bash
git add app/services/ops_monitor.py tests/unit/test_ops_monitor.py
git commit -m "feat(ops): OpsMonitorService snapshot-integrity + overnight position anomaly

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: OpsMonitorService — 管线运行重建 + 数据新鲜度

**Files:**
- Modify: `app/services/ops_monitor.py`
- Test: `tests/unit/test_ops_monitor.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
def test_pipeline_runs_marks_missing_weekday(sf):
    inst = "paper_v20h_v20h_v1_3"
    from app.models import RawSignal
    with sf() as s:
        # 20260528 周四有信号；20260529 周五没有 → 应标 missing
        s.add(RawSignal(signal_id="a", instance_id=inst, symbol="600000.SH",
                        direction="SELL", quantity=100, reference_price=1.0,
                        price_offset=0.0, limit_price=1.0, valid_date="20260528",
                        signal_time="2026-05-28T16:00:00+08:00", precheck_status="PASS"))
        s.commit()
    svc = OpsMonitorService(sf)
    runs = svc.pipeline_runs(lookback_days=4, today="20260601")
    by = {r["valid_date"]: r for r in runs}
    assert by["20260528"]["status"] == "ok"
    assert by["20260529"]["status"] == "missing"   # 周五无信号

def test_data_freshness_reports_lag(sf, tmp_path):
    import pandas as pd
    from app.storage.parquet import ParquetStore
    store = ParquetStore(root=tmp_path / "data")
    store.append("indexes", "000852.SH", pd.DataFrame([{"trade_date": 20260430, "open":1,"high":1,"low":1,"close":1,"volume":1}]))
    svc = OpsMonitorService(sf, parquet_store=store)
    fr = svc.data_freshness(today="20260608", probe=("indexes", "000852.SH"))
    assert fr["market_latest"] == 20260430
    assert fr["market_lag_days"] == 39
```

- [ ] **Step 2: 运行确认失败** — FAIL（无 pipeline_runs/data_freshness）。

- [ ] **Step 3: 追加实现到 `OpsMonitorService`**

```python
    def pipeline_runs(self, lookback_days: int = 14, today: str | None = None) -> list[dict]:
        """逐(工作日)重建管线是否运行：有 raw_signals(该 valid_date)=ok；周末=skip-weekend；
        交易日无信号=missing。orders 计数辅助。"""
        end = _d(today) if today else datetime.now().date()
        days = [(end - timedelta(days=k)) for k in range(lookback_days)]
        out = []
        with self.sf() as s:
            for dd in sorted(days):
                vd = dd.strftime("%Y%m%d")
                weekday = dd.isoweekday() <= 5
                sig = s.execute(select(RawSignal.signal_time)
                                .where(RawSignal.valid_date == vd).limit(1)).first()
                norders = s.execute(
                    select(__import__("sqlalchemy").func.count()).select_from(Order)
                    .where(Order.valid_date == vd)).scalar() or 0
                if not weekday:
                    status = "weekend"
                elif sig:
                    status = "ok"
                else:
                    status = "missing"
                out.append({"valid_date": vd, "weekday": weekday, "status": status,
                            "signal_time": sig[0] if sig else None, "orders": int(norders)})
        return out

    def data_freshness(self, today: str | None = None,
                       probe: tuple[str, str] = ("indexes", "000852.SH")) -> dict:
        t = _d(today) if today else datetime.now().date()
        latest = self.store.latest_date(*probe) if self.store else None
        lag = (t - _d(str(latest))).days if latest else None
        return {"market_latest": latest, "market_lag_days": lag,
                "probe": f"{probe[0]}/{probe[1]}"}
```

- [ ] **Step 4: 运行确认通过** — PASS。

- [ ] **Step 5: Commit**

```bash
git add app/services/ops_monitor.py tests/unit/test_ops_monitor.py
git commit -m "feat(ops): pipeline-run reconstruction + data-freshness probe

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: AlertEngine — 检查套件 + AlertSink 接口

**Files:**
- Create: `app/services/alerts.py`
- Test: `tests/unit/test_alerts.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_alerts.py
import json, pytest
from app.db import init_db, make_engine, make_session_factory
from app.models import PerfSnapshot
from app.services.ops_monitor import OpsMonitorService
from app.services.alerts import AlertEngine, DashboardSink

@pytest.fixture
def sf(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path}/t.db"); init_db(eng)
    return make_session_factory(eng)

def test_alert_engine_flags_overnight_doubling_critical(sf):
    inst = "paper_v53_v53"
    with sf() as s:
        s.add(PerfSnapshot(instance_id=inst, date="20260608", nav=9_888_426, daily_return=-0.0036,
                           positions_snapshot=json.dumps({"511260.SH": 49500})))
        s.add(PerfSnapshot(instance_id=inst, date="20260609", nav=16_608_072, daily_return=0.68,
                           positions_snapshot=json.dumps({"511260.SH": 99000})))
        s.commit()
    eng = AlertEngine(OpsMonitorService(sf), instances=[inst])
    alerts = eng.run_checks(today="20260609")
    crit = [a for a in alerts if a.severity == "critical" and a.category == "position_anomaly"]
    assert crit and "511260.SH" in crit[0].message

def test_dashboard_sink_stores_latest():
    sink = DashboardSink()
    from app.services.alerts import Alert
    sink.emit([Alert(id="x", severity="warn", category="c", message="m", as_of="t")])
    assert len(sink.latest()) == 1
```

- [ ] **Step 2: 运行确认失败** — FAIL（无 alerts 模块）。

- [ ] **Step 3: 实现 `app/services/alerts.py`**

```python
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
                 overnight_threshold: float = 0.5, market_lag_warn: int = 3):
        self.ops = ops
        self.instances = instances
        self.sinks = sinks or []
        self.overnight_threshold = overnight_threshold
        self.market_lag_warn = market_lag_warn

    def run_checks(self, today: str | None = None) -> list[Alert]:
        alerts: list[Alert] = []
        # 1. 隔夜持仓异常（按实例）
        for inst in self.instances:
            for a in self.ops.overnight_position_anomalies(inst, self.overnight_threshold):
                alerts.append(Alert(
                    id=f"posanom:{inst}:{a['symbol']}:{a['to_date']}",
                    severity="critical", category="position_anomaly",
                    message=f"{inst} {a['symbol']} 隔夜 {a['prev_qty']:.0f}→{a['cur_qty']:.0f}"
                            f"（×{a['ratio']}）",
                    as_of=a["to_date"], detail=a))
            for iss in self.ops.snapshot_integrity(inst)["issues"]:
                alerts.append(Alert(
                    id=f"frozen:{inst}:{iss['date']}", severity="warn",
                    category="snapshot_integrity",
                    message=f"{inst} NAV 快照疑似冻结 @ {iss['date']}",
                    as_of=iss["date"], detail=iss))
        # 2. 数据新鲜度
        if self.ops.store is not None:
            fr = self.ops.data_freshness(today=today)
            if fr["market_lag_days"] is not None and fr["market_lag_days"] > self.market_lag_warn:
                alerts.append(Alert(
                    id=f"stale_market:{fr['market_latest']}", severity="warn",
                    category="data_freshness",
                    message=f"行情陈旧 {fr['market_lag_days']} 天（latest {fr['market_latest']}）",
                    as_of=str(fr["market_latest"]), detail=fr))
        # 3. 管线今日/最近交易日是否缺失
        runs = self.ops.pipeline_runs(lookback_days=5, today=today)
        missing = [r for r in runs if r["status"] == "missing"]
        if missing:
            last = missing[-1]
            alerts.append(Alert(
                id=f"pipeline_missing:{last['valid_date']}", severity="critical",
                category="pipeline",
                message=f"管线缺失运行：{', '.join(r['valid_date'] for r in missing)}",
                as_of=last["valid_date"], detail={"missing": [r["valid_date"] for r in missing]}))
        for s in self.sinks:
            s.emit(alerts)
        return alerts
```

- [ ] **Step 4: 运行确认通过** — `pytest tests/unit/test_alerts.py -q` → PASS。

- [ ] **Step 5: Commit**

```bash
git add app/services/alerts.py tests/unit/test_alerts.py
git commit -m "feat(ops): AlertEngine check-suite + Alert/AlertSink/DashboardSink (WeChat sink reserved)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: API 端点 — /admin/ops/*、/admin/alerts、/admin/dashboard-meta

**Files:**
- Create: `app/api/ops.py`
- Modify: `app/main.py`（注册路由）
- Modify: `app/dependencies.py`（`get_ops_monitor`、`get_alert_engine`）
- Test: `tests/unit/test_api_ops.py`

- [ ] **Step 1: 写失败测试（用 TestClient + override settings/data）**

```python
# tests/unit/test_api_ops.py
import json
import pandas as pd
from app.models import PerfSnapshot
from app.db import make_engine, make_session_factory, init_db

def _seed(settings):
    eng = make_engine(settings.db_url); init_db(eng); sf = make_session_factory(eng)
    with sf() as s:
        s.add(PerfSnapshot(instance_id="paper_v53_v53", date="20260608", nav=9_888_426,
                           daily_return=-0.0036, positions_snapshot=json.dumps({"511260.SH": 49500})))
        s.add(PerfSnapshot(instance_id="paper_v53_v53", date="20260609", nav=16_608_072,
                           daily_return=0.68, positions_snapshot=json.dumps({"511260.SH": 99000})))
        s.commit()

def test_dashboard_meta_and_alerts(client, settings_for_test):
    _seed(settings_for_test)
    h = {"Authorization": "Bearer TEST_KEY"}
    meta = client.get("/admin/dashboard-meta", headers=h).json()
    assert meta["code"] == 0
    assert "version" in meta["data"] and "alerts" in meta["data"]
    al = client.get("/admin/alerts", headers=h).json()
    cats = [a["category"] for a in al["data"]["alerts"]]
    assert "position_anomaly" in cats

def test_ops_endpoints_authed(client, settings_for_test):
    _seed(settings_for_test)
    h = {"Authorization": "Bearer TEST_KEY"}
    for path in ["/admin/ops/pipeline-runs", "/admin/ops/snapshot-integrity?instance_id=paper_v53_v53",
                 "/admin/ops/reconcile-anomalies?instance_id=paper_v53_v53"]:
        r = client.get(path, headers=h)
        assert r.status_code == 200 and r.json()["code"] == 0
```

（注：`client`/`settings_for_test` 来自 `tests/conftest.py`；instances 列表端点从 `instance_state`/strategies 取，测试里没 instance_state 行时应优雅返回空而非报错——实现需容错。）

- [ ] **Step 2: 运行确认失败** — FAIL（无端点）。

- [ ] **Step 3: 依赖注入（追加到 `app/dependencies.py`）**

```python
from app.services.ops_monitor import OpsMonitorService
from app.services.alerts import AlertEngine

def get_ops_monitor(
    sf=Depends(get_session_factory),
    store: ParquetStore = Depends(get_parquet_store),
    settings: Settings = Depends(get_settings),
) -> OpsMonitorService:
    return OpsMonitorService(sf, parquet_store=store, settings=settings)

def get_alert_engine(
    ops: OpsMonitorService = Depends(get_ops_monitor),
    sf=Depends(get_session_factory),
) -> AlertEngine:
    from sqlalchemy import select
    from app.models import InstanceState
    with sf() as s:
        insts = [r[0] for r in s.execute(select(InstanceState.instance_id)).all()]
    return AlertEngine(ops, instances=insts)
```

- [ ] **Step 4: 实现 `app/api/ops.py`**

```python
"""运营/对账/告警端点。轻薄：调用 OpsMonitorService / AlertEngine。"""
from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc
from app.api.auth import verify_api_key            # 按现有 verify_api_key 位置调整
from app.exceptions import APIResponse              # 按现有 APIResponse 位置调整
from app.dependencies import (get_ops_monitor, get_alert_engine, get_session_factory)
from app.models import PerfSnapshot, InstanceState
from app.services.ops_monitor import OpsMonitorService
from app.services.alerts import AlertEngine

router = APIRouter(prefix="/admin")

@router.get("/ops/pipeline-runs", response_model=APIResponse[dict],
            dependencies=[Depends(verify_api_key)])
async def ops_pipeline_runs(days: int = Query(14, ge=1, le=60),
                            ops: OpsMonitorService = Depends(get_ops_monitor)):
    return APIResponse[dict](code=0, message="ok", data={"runs": ops.pipeline_runs(days)})

@router.get("/ops/snapshot-integrity", response_model=APIResponse[dict],
            dependencies=[Depends(verify_api_key)])
async def ops_snapshot_integrity(instance_id: str, lookback: int = Query(30, ge=2, le=400),
                                 ops: OpsMonitorService = Depends(get_ops_monitor)):
    return APIResponse[dict](code=0, message="ok", data=ops.snapshot_integrity(instance_id, lookback))

@router.get("/ops/reconcile-anomalies", response_model=APIResponse[dict],
            dependencies=[Depends(verify_api_key)])
async def ops_reconcile_anomalies(instance_id: str,
                                  threshold: float = Query(0.5, gt=0),
                                  ops: OpsMonitorService = Depends(get_ops_monitor)):
    return APIResponse[dict](code=0, message="ok",
        data={"overnight_position_anomalies": ops.overnight_position_anomalies(instance_id, threshold)})

@router.get("/alerts", response_model=APIResponse[dict], dependencies=[Depends(verify_api_key)])
async def alerts(eng: AlertEngine = Depends(get_alert_engine)):
    al = eng.run_checks()
    return APIResponse[dict](code=0, message="ok",
        data={"alerts": [a.to_dict() for a in al],
              "counts": {"critical": sum(a.severity == "critical" for a in al),
                         "warn": sum(a.severity == "warn" for a in al),
                         "info": sum(a.severity == "info" for a in al)}})

@router.get("/dashboard-meta", response_model=APIResponse[dict], dependencies=[Depends(verify_api_key)])
async def dashboard_meta(ops: OpsMonitorService = Depends(get_ops_monitor),
                         eng: AlertEngine = Depends(get_alert_engine),
                         sf=Depends(get_session_factory)):
    al = eng.run_checks()
    fr = ops.data_freshness()
    with sf() as s:
        rows = s.execute(select(PerfSnapshot.instance_id, PerfSnapshot.date, PerfSnapshot.nav)
                         .order_by(desc(PerfSnapshot.date))).all()
        max_perf = rows[0][1] if rows else None
        navs = {}
        for inst, d, nav in rows:
            navs.setdefault(inst, (d, nav))
    runs = ops.pipeline_runs(5)
    last_run = next((r for r in reversed(runs) if r["status"] == "ok"), None)
    return APIResponse[dict](code=0, message="ok", data={
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "freshness": fr,
        "last_pipeline_run": last_run,
        "account_nav": sum(v[1] for v in navs.values()) if navs else None,
        "instance_navs": {k: {"date": v[0], "nav": v[1]} for k, v in navs.items()},
        "alerts": {"critical": sum(a.severity == "critical" for a in al),
                   "warn": sum(a.severity == "warn" for a in al)},
        "version": {"max_perf_date": max_perf,
                    "last_run_signal_time": last_run["signal_time"] if last_run else None,
                    "alert_rev": len(al)},
    })
```
> 注：`verify_api_key` 与 `APIResponse` 的真实 import 路径以 `app/api/admin_query.py` 顶部为准（实现时先 grep 确认），别凭空。

- [ ] **Step 5: 注册路由（`app/main.py`）** — 在 `from app.api import ...` 加 `ops`，并 `app.include_router(ops.router, tags=["ops"])`（紧随 admin_query）。

- [ ] **Step 6: 运行确认通过** — `pytest tests/unit/test_api_ops.py -q` → PASS；再跑全量 `pytest tests/unit -q` 确认无新增回归（已知 6 个 blacklist 失败为 pre-existing）。

- [ ] **Step 7: Commit**

```bash
git add app/api/ops.py app/main.py app/dependencies.py tests/unit/test_api_ops.py
git commit -m "feat(ops): /admin/ops/* + /admin/alerts + /admin/dashboard-meta endpoints

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: 前端 — 共享 JS 骨架 + header 健康 strip

**Files:**
- Modify: `app/api/dashboard.py`（`_DASHBOARD_HTML`）

> 前端无单测；验证 = 起服务器载入 `/dashboard` 目视 + 用已 seed 的测试库确认面板渲染。

- [ ] **Step 1: 加共享 JS 骨架**（在现有 `<script>` 顶部，`api()` 之后）：
  - `fmtPct(x)`, `fmtNum(x)`, `agoBadge(asOf)`（返回 as-of + 陈旧高亮 class：>1 交易日琥珀、冻结/critical 红）。
  - `RefreshScheduler`：注册 `(key, fn, intervalMs)`；`metaPoll` 每 15000ms 调 `/admin/dashboard-meta`，把 `version` 缓存，version 变化时触发已注册的「分析层」刷新；分析层默认只在 tab 打开 + version 变时刷新。
  - `renderAlertBadge(meta)`：header 右上角红/琥珀 badge 显示 critical/warn 数。

- [ ] **Step 2: 加常驻 header 健康 strip**（紧贴现有 `.header` 下方，新增 `<div id="health-strip" class="health-strip">`）。渲染函数 `renderHealthStrip(meta)` 展示：管线 last-run（日期+「今日是否已跑」红/绿点）、行情 as-of + lag 徽标、pred lag、账户合并 NAV、各实例 NAV(date)、critical/warn badge。CSS 复用现有 `.kpi`/`.badge` 风格，加 `.dot.ok{background:#4ade80}` `.dot.bad{background:#f87171}`。

- [ ] **Step 3: 接线** — 页面载入时 `metaPoll()` 立即跑一次并 `setInterval(metaPoll, 15000)`；把现有 `setInterval(refreshAll, 60000)` 改为：分析层不再无脑 60s，由 RefreshScheduler 在 version 变化/tab 切换时驱动（保留手动「刷新」按钮立即 refreshAll 当前 tab）。

- [ ] **Step 4: 验证（manual）**

```bash
cd /Users/mameican/Desktop/server/v2.3/server
QMT_API_KEY=TEST_KEY QMT_DB_URL="sqlite:///$PWD/pipeline-server.db" \
  /Users/mameican/Desktop/server/venv/bin/python -m uvicorn app.main:app --port 8099 &
sleep 3
curl -s -H "Authorization: Bearer TEST_KEY" http://127.0.0.1:8099/admin/dashboard-meta | head -c 400
# 浏览器开 http://127.0.0.1:8099/dashboard，输入 key，确认 header strip 出现、15s 自刷新、badge 正常
kill %1
```
Expected：dashboard-meta 返回 200 + JSON；页面顶部出现健康 strip，pred/行情陈旧时徽标变色，有 critical 告警时红 badge。

- [ ] **Step 5: Commit**

```bash
git add app/api/dashboard.py
git commit -m "feat(dashboard): shared JS skeleton (tiered refresh + as-of/staleness) + header health strip

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 前端 — 「运营与对账」tab + 告警 feed

**Files:**
- Modify: `app/api/dashboard.py`

- [ ] **Step 1: 加 tab** — 在 tabs 栏加 `<div class="tab" data-view="ops"><span class="icon">🛠</span>运营与对账</div>` 和对应 `<div class="view" id="view-ops">`，内含 5 张 card：告警 feed / 管线运行日志 / 数据新鲜度 / 快照完整性 / 对账异常。

- [ ] **Step 2: 渲染函数 `loadOps()`** 并发拉：
```javascript
const [alerts, runs, integ, anom] = await Promise.all([
  api('/admin/alerts'),
  api('/admin/ops/pipeline-runs?days=14'),
  api('/admin/ops/snapshot-integrity?instance_id=' + getInstanceId() + '&lookback=30'),
  api('/admin/ops/reconcile-anomalies?instance_id=' + getInstanceId()),
]);
```
渲染：
  - **告警 feed**：按 severity 排序，critical 红/warn 琥珀/info 灰，显示 message + as_of。空则「✓ 无告警」。
  - **管线运行日志**：表格 valid_date / status(ok 绿 / missing 红 / weekend 灰) / signal_time / orders。
  - **数据新鲜度**：行情 latest + lag 徽标、pred lag（从 meta 缓存取）。
  - **快照完整性**：列 frozen 问题日期；空则「✓ 无冻结/缺口」。
  - **对账异常**：隔夜持仓异常表（symbol / prev→cur / ×ratio，critical 高亮）；空则「✓ 无隔夜异常」。

- [ ] **Step 3: 注册到刷新调度** — ops tab 归「运营层」：打开即拉 + 随 metaPoll(15s) version 变化刷新。

- [ ] **Step 4: 验证（manual）** — 用 Task 5 的本地起服 + seed（把 Task 4 测试里的两条 511260 快照灌进本地 pipeline-server.db），开 `/dashboard` → 运营 tab：应看到「511260.SH 隔夜 ×2 critical」告警 + 对账异常表那一行 + 管线日志。改 seed 让某周五无信号 → 应看到 missing + critical 管线告警。

```bash
# seed 本地库（示例）
/Users/mameican/Desktop/server/venv/bin/python - <<'PY'
import sqlite3, json
c=sqlite3.connect("v2.3/server/pipeline-server.db")
# 视实际表结构 insert 两条 perf_snapshots（paper_v53_v53 0608=49500 / 0609=99000）
PY
```

- [ ] **Step 5: Commit**

```bash
git add app/api/dashboard.py
git commit -m "feat(dashboard): 运营与对账 tab + alert feed (catches stop/frozen/doubling incidents)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review 记录

- **Spec 覆盖（阶段一）**：健康 strip→Task5；运营 tab→Task6；pipeline 运行日志→Task2/6；快照完整性→Task1/6；隔夜 |Δqty| tripwire→Task1/3/6；数据新鲜度→Task2/5；告警 feed + AlertSink 预留→Task3/4/6；dashboard-meta + 版本令牌 + 分层刷新→Task4/5；as-of/陈旧标记→Task5。✔ 阶段二/三（归因/IC/风险/总览）按 spec 不在本计划。
- **未入账分红**：阶段一以「现金分叉」通用告警覆盖（reconcile/bookkeeping-divergence），精确分红归因留阶段二——已在 spec §5 标注，本计划不单列，避免 scope 蔓延。
- **占位符**：后端 task 均含真实 test+impl 代码与确切命令；前端 task 因 UI 不做单测，给出确切插入点 + 关键 JS + 可执行的 manual 验证（起服+seed+目视/curl），非「TODO」。两处显式「实现时先 grep 确认」：`verify_api_key`/`APIResponse` 的 import 路径、本地 perf_snapshots insert 列——因这些是既有代码事实、需按真实文件确认，不可凭空写死。
- **类型一致**：`Alert`(id/severity/category/message/as_of/detail) 跨 Task3/4/6 一致；`OpsMonitorService` 方法名 snapshot_integrity/overnight_position_anomalies/pipeline_runs/data_freshness 跨 Task1/2/3/4 一致；端点路径与前端 fetch 一致。✔
- **风险**：`verify_api_key`/`APIResponse` import 路径需按 admin_query.py 实际确认（已标注）；多 worker 下 DashboardSink 内存态不共享——本阶段 /admin/alerts 每次重算不依赖 sink 缓存（sink 仅为推送预留），不影响正确性。
