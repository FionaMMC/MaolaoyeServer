"""Admin 只读查询端点：把 SQLite 关键表暴露成 HTTP API。

用户从 Mac 端 curl 就能拉到所有诊断数据，不需要 ssh + sqlite。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select

from app.auth import AuthContext, verify_api_key
from app.dependencies import (
    get_blacklist_service,
    get_data_upload_service,
    get_metrics_service,
    get_reconcile_service,
    get_session_factory,
    get_settings,
)
from app.models import (
    InstanceState,
    Order,
    PerfSnapshot,
    RawSignal,
    ShadowInstanceState,
    ShadowNavSnapshot,
    Trade,
)
from app.schemas.common import APIResponse
from app.schemas.reconcile import QmtPositionSnapshot, ReconcileResult
from app.services.blacklist import BlacklistService
from app.services.data_upload import DataUploadService
from app.services.metrics import MetricsService, date_range_for_period
from app.services.reconcile import (
    InstanceNotFound,
    ReconcileSanityCheckFailed,
    ReconcileService,
)
from app.settings import Settings

router = APIRouter(prefix="/admin")

logger = logging.getLogger(__name__)


def _configured_regular_instances(settings: Settings) -> dict[str, dict] | None:
    """Return configured regular instances, or None when no config is available."""
    path = Path(settings.strategies_file)
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    result: dict[str, dict] = {}
    for group in config.get("account_groups", []):
        group_id = group["group_id"]
        for strategy in group.get("strategies", []):
            instance_id = f"{group_id}_{strategy['strategy_id']}"
            result[instance_id] = {
                "display_name": strategy.get("display_name", instance_id),
                "orders_enabled": bool(strategy.get("orders_enabled", True)),
            }
    return result


@router.get(
    "/shadow/summary",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def shadow_summary(sf=Depends(get_session_factory)):
    """Read-only shadow health/NAV comparison; never exposes order actions."""
    with sf() as session:
        states = session.execute(
            select(ShadowInstanceState).order_by(ShadowInstanceState.shadow_id)
        ).scalars().all()
        items = []
        for state in states:
            latest = session.execute(
                select(ShadowNavSnapshot)
                .where(ShadowNavSnapshot.shadow_id == state.shadow_id)
                .order_by(ShadowNavSnapshot.date.desc())
                .limit(1)
            ).scalar_one_or_none()
            items.append({
                "shadow_id": state.shadow_id,
                "instance_type": "shadow_no_order",
                "status": state.status,
                "nav": latest.nav if latest else None,
                "daily_return": latest.daily_return if latest else None,
                "date": latest.date if latest else None,
                "cash": state.virtual_cash,
                "holdings_count": len(state.virtual_positions or {}),
                "turnover": latest.turnover if latest else state.last_turnover,
                "cumulative_cost": state.cumulative_cost,
                "decision_date": state.decision_date,
                "as_of_date": state.as_of_date,
                "state_reason": state.state_reason,
                "source_version": state.source_version,
                "input_hash": state.input_hash,
                "target_hash": state.target_hash,
                "orders_enabled": False,
            })
    return APIResponse[dict](code=0, message="ok", data={"items": items})


@router.get(
    "/shadow/nav-history",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def shadow_nav_history(
    shadow_id: str,
    limit: int = Query(300, ge=1, le=2000),
    sf=Depends(get_session_factory),
):
    with sf() as session:
        rows = session.execute(
            select(ShadowNavSnapshot)
            .where(ShadowNavSnapshot.shadow_id == shadow_id)
            .order_by(ShadowNavSnapshot.date.desc())
            .limit(limit)
        ).scalars().all()
        items = [{
            "shadow_id": row.shadow_id, "date": row.date, "nav": row.nav,
            "daily_return": row.daily_return, "cash": row.virtual_cash,
            "positions": row.positions_snapshot, "transaction_cost": row.transaction_cost,
            "turnover": row.turnover, "decision_date": row.decision_date,
            "as_of_date": row.as_of_date, "state_reason": row.state_reason,
            "source_version": row.source_version, "input_hash": row.input_hash,
            "target_hash": row.target_hash,
        } for row in rows]
    return APIResponse[dict](code=0, message="ok", data={"items": items})


# ── 1. /admin/orders : 完整 orders 查询 ───────────────────────────────
@router.get(
    "/orders",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def admin_orders(
    date: str | None = Query(None, min_length=8, max_length=8, pattern=r"^\d{8}$"),
    status: str | None = Query(None, pattern=r"^(PENDING|FILLED|PARTIAL|CANCELLED|REJECTED)$"),
    account_group: str | None = None,
    direction: str | None = Query(None, pattern=r"^(BUY|SELL)$"),
    symbol: str | None = None,
    limit: int = Query(200, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    sf=Depends(get_session_factory),
):
    """按条件筛 orders。不指定 status 时返回**所有**状态（不像 /orders 只返 PENDING）。"""
    with sf() as session:
        stmt = select(Order)
        if date:
            stmt = stmt.where(Order.valid_date == date)
        if status:
            stmt = stmt.where(Order.status == status)
        if account_group:
            stmt = stmt.where(Order.account_group == account_group)
        if direction:
            stmt = stmt.where(Order.direction == direction)
        if symbol:
            stmt = stmt.where(Order.symbol == symbol)
        total = session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar()
        stmt = stmt.order_by(desc(Order.created_at)).limit(limit).offset(offset)
        rows = session.execute(stmt).scalars().all()
        items = [
            {
                "order_id": r.order_id,
                "account_group": r.account_group,
                "symbol": r.symbol,
                "direction": r.direction,
                "quantity": r.quantity,
                "limit_price": r.limit_price,
                "valid_date": r.valid_date,
                "status": r.status,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    return APIResponse[dict](
        code=0, message="ok",
        data={
            "total": total,
            "limit": limit,
            "offset": offset,
            "returned": len(items),
            "items": items,
        },
    )


# ── 2. /admin/orders-summary : 按日 + status + 方向 聚合 ─────────────────
@router.get(
    "/orders-summary",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def orders_summary(
    date: str | None = Query(None, min_length=8, max_length=8, pattern=r"^\d{8}$"),
    days: int = Query(7, ge=1, le=365),
    sf=Depends(get_session_factory),
):
    """orders 聚合视图。
    - 不带 date：返回最近 N 天每天的 status × direction 矩阵
    - 带 date：返回该天细节
    """
    if date:
        cutoff = date
        end_date = date
    else:
        end_date = datetime.now().strftime("%Y%m%d")
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    with sf() as session:
        stmt = (
            select(
                Order.valid_date,
                Order.account_group,
                Order.direction,
                Order.status,
                func.count().label("n"),
            )
            .where(Order.valid_date >= cutoff)
            .where(Order.valid_date <= end_date)
            .group_by(Order.valid_date, Order.account_group, Order.direction, Order.status)
            .order_by(Order.valid_date, Order.account_group, Order.direction, Order.status)
        )
        rows = session.execute(stmt).all()

    summary: dict = {}
    for vd, ag, d, st, n in rows:
        summary.setdefault(vd, {}).setdefault(ag, {}).setdefault(d, {})[st] = n
    return APIResponse[dict](
        code=0, message="ok",
        data={"cutoff": cutoff, "end_date": end_date, "by_date": summary},
    )


# ── 3. /admin/nav-history : perf_snapshots ─────────────────────────────
@router.get(
    "/nav-history",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def nav_history(
    instance_id: str | None = None,
    period: str | None = Query(None, pattern=r"^(7d|30d|90d|180d|1y|ytd|all)$"),
    limit: int = Query(60, ge=1, le=1000),
    sf=Depends(get_session_factory),
):
    """返回正式或影子实例指定时间窗内最近 N 条 NAV。"""
    with sf() as session:
        is_shadow = bool(instance_id and instance_id.startswith("Shadow_"))
        model = ShadowNavSnapshot if is_shadow else PerfSnapshot
        id_column = model.shadow_id if is_shadow else model.instance_id
        stmt = select(model).order_by(desc(model.date), id_column)
        if instance_id:
            stmt = stmt.where(id_column == instance_id)
        if period:
            stmt = stmt.where(model.date >= date_range_for_period(period))
        stmt = stmt.limit(limit)
        rows = session.execute(stmt).scalars().all()
        items = [
            {
                "instance_id": r.shadow_id if is_shadow else r.instance_id,
                "date": r.date,
                "nav": r.nav,
                "daily_return": r.daily_return,
                "positions_count": (
                    len(r.positions_snapshot) if isinstance(r.positions_snapshot, dict) else 0
                ),
            }
            for r in rows
        ]
    return APIResponse[dict](
        code=0, message="ok",
        data={"instance_id": instance_id, "period": period, "count": len(items), "items": items},
    )


# ── 4. /admin/instance-state ────────────────────────────────────────────
@router.get(
    "/instance-state",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def instance_state(
    instance_id: str | None = None,
    include_positions: bool = Query(False, description="true 时返回 {symbol: qty} 详情"),
    sf=Depends(get_session_factory),
):
    """所有 instance 的当前 cash + 持仓数（可选返回完整持仓 dict）。"""
    with sf() as session:
        stmt = select(InstanceState)
        if instance_id:
            stmt = stmt.where(InstanceState.instance_id == instance_id)
        rows = session.execute(stmt).scalars().all()
        items = []
        for r in rows:
            positions = r.virtual_positions or {}
            item = {
                "instance_id": r.instance_id,
                "virtual_cash": r.virtual_cash,
                "holdings_count": len(positions),
                "total_shares": sum(positions.values()) if positions else 0,
                "last_update": r.last_update,
            }
            if include_positions:
                item["virtual_positions"] = positions
            items.append(item)
    return APIResponse[dict](code=0, message="ok", data={"count": len(items), "items": items})


@router.get(
    "/portfolio-overview",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def portfolio_overview(
    sf=Depends(get_session_factory),
    settings: Settings = Depends(get_settings),
):
    """Return regular and no-order shadow ledgers in one selectable catalogue."""
    configured = _configured_regular_instances(settings)
    with sf() as session:
        items = []
        for state in session.execute(select(InstanceState)).scalars().all():
            if configured is not None and state.instance_id not in configured:
                continue
            instance_cfg = (configured or {}).get(state.instance_id, {})
            latest = session.execute(
                select(PerfSnapshot)
                .where(PerfSnapshot.instance_id == state.instance_id)
                .order_by(desc(PerfSnapshot.date))
                .limit(1)
            ).scalar_one_or_none()
            items.append({
                "instance_id": state.instance_id,
                "display_name": instance_cfg.get("display_name", state.instance_id),
                "virtual_cash": state.virtual_cash,
                "holdings_count": len(state.virtual_positions or {}),
                "latest_nav": latest.nav if latest else None,
                "latest_nav_date": latest.date if latest else None,
                "latest_daily_return": latest.daily_return if latest else None,
                "last_update": state.last_update,
                "is_shadow": False,
                "orders_enabled": instance_cfg.get("orders_enabled", True),
            })

        for state in session.execute(select(ShadowInstanceState)).scalars().all():
            latest = session.execute(
                select(ShadowNavSnapshot)
                .where(ShadowNavSnapshot.shadow_id == state.shadow_id)
                .order_by(desc(ShadowNavSnapshot.date))
                .limit(1)
            ).scalar_one_or_none()
            items.append({
                "instance_id": state.shadow_id,
                "virtual_cash": state.virtual_cash,
                "holdings_count": len(state.virtual_positions or {}),
                "latest_nav": latest.nav if latest else None,
                "latest_nav_date": latest.date if latest else None,
                "latest_daily_return": latest.daily_return if latest else None,
                "last_update": state.last_update,
                "is_shadow": True,
                "orders_enabled": False,
            })

    return APIResponse[dict](
        code=0,
        message="ok",
        data={
            "items": items,
            "reported_virtual_nav_sum": sum(item["latest_nav"] or 0.0 for item in items),
            "reported_virtual_nav_sum_is_account_nav": False,
            "note": "Each row is an independent virtual ledger; shadow rows can never create orders.",
        },
    )


# ── 5. /admin/trades ──────────────────────────────────────────────────────
@router.get(
    "/trades",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def trades(
    date: str | None = Query(None, min_length=8, max_length=8, pattern=r"^\d{8}$"),
    order_id: str | None = None,
    status: str | None = None,
    limit: int = Query(200, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    sf=Depends(get_session_factory),
):
    """成交回报表。日期用 received_at 前 8 位匹配。"""
    with sf() as session:
        stmt = select(Trade)
        if date:
            stmt = stmt.where(Trade.received_at.like(f"{date[:4]}-{date[4:6]}-{date[6:8]}%"))
        if order_id:
            stmt = stmt.where(Trade.order_id == order_id)
        if status:
            stmt = stmt.where(Trade.status == status)
        total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar()
        stmt = stmt.order_by(desc(Trade.id)).limit(limit).offset(offset)
        rows = session.execute(stmt).scalars().all()
        items = [
            {
                "id": r.id,
                "order_id": r.order_id,
                "filled_quantity": r.filled_quantity,
                "filled_price": r.filled_price,
                "filled_time": r.filled_time,
                "status": r.status,
                "received_at": r.received_at,
            }
            for r in rows
        ]
    return APIResponse[dict](
        code=0, message="ok",
        data={"total": total, "limit": limit, "offset": offset, "returned": len(items), "items": items},
    )


# ── 6. /admin/signals : raw_signals 查询 ──────────────────────────────
@router.get(
    "/signals",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def signals(
    date: str | None = Query(None, min_length=8, max_length=8, pattern=r"^\d{8}$"),
    instance_id: str | None = None,
    precheck_status: str | None = Query(None, pattern=r"^(PASS|FAIL)$"),
    symbol: str | None = None,
    limit: int = Query(200, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    sf=Depends(get_session_factory),
):
    """原始 signals 表（含预检状态 + 拒绝原因）。"""
    with sf() as session:
        stmt = select(RawSignal)
        if date:
            stmt = stmt.where(RawSignal.valid_date == date)
        if instance_id:
            stmt = stmt.where(RawSignal.instance_id == instance_id)
        if precheck_status:
            stmt = stmt.where(RawSignal.precheck_status == precheck_status)
        if symbol:
            stmt = stmt.where(RawSignal.symbol == symbol)
        total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar()
        stmt = stmt.order_by(desc(RawSignal.signal_time)).limit(limit).offset(offset)
        rows = session.execute(stmt).scalars().all()
        items = [
            {
                "signal_id": r.signal_id,
                "instance_id": r.instance_id,
                "symbol": r.symbol,
                "direction": r.direction,
                "quantity": r.quantity,
                "reference_price": r.reference_price,
                "limit_price": r.limit_price,
                "valid_date": r.valid_date,
                "signal_time": r.signal_time,
                "precheck_status": r.precheck_status,
                "precheck_reason": r.precheck_reason,
            }
            for r in rows
        ]
    return APIResponse[dict](
        code=0, message="ok",
        data={"total": total, "limit": limit, "offset": offset, "returned": len(items), "items": items},
    )


# ── 7. /admin/health : 综合健康度 ───────────────────────────────────────
@router.get(
    "/health",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def admin_health(
    sf=Depends(get_session_factory),
    bl: BlacklistService = Depends(get_blacklist_service),
    settings: Settings = Depends(get_settings),
    upload: DataUploadService = Depends(get_data_upload_service),
):
    """一站式健康度：pred 新鲜度、黑名单大小、PENDING 数量、各 instance NAV。"""
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

    # pred 文件新鲜度
    pred_status = None
    try:
        info = upload.status(strategy_name="v20h_v1_3")
        files = info.get("files", [])
        if files:
            pred_file = next((f for f in files if f["filename"] == "pred_csi1000.parquet"), None)
            if pred_file:
                mtime_str = pred_file.get("mtime", "")
                try:
                    mtime = datetime.fromisoformat(mtime_str)
                    lag_hours = (datetime.now(mtime.tzinfo) - mtime).total_seconds() / 3600
                except Exception:
                    lag_hours = None
                pred_status = {
                    "mtime": mtime_str,
                    "lag_hours": round(lag_hours, 1) if lag_hours else None,
                    "size_mb": round(pred_file["bytes"] / 1024 / 1024, 1),
                }
    except Exception as e:
        pred_status = {"error": str(e)}

    # blacklist
    bl_stats = bl.stats(lookback_days=30)
    bl_summary = {
        "auto_unique": bl_stats["auto_unique_symbols"],
        "manual_count": bl_stats["manual_count"],
        "merged_total": bl_stats["merged_total"],
    }

    # PENDING orders
    configured = _configured_regular_instances(settings)
    with sf() as session:
        pending_count = session.execute(
            select(func.count()).select_from(Order).where(Order.status == "PENDING")
        ).scalar()

        # 最近一周 status 矩阵
        status_breakdown = session.execute(
            select(Order.valid_date, Order.status, func.count())
            .where(Order.valid_date >= week_ago)
            .group_by(Order.valid_date, Order.status)
            .order_by(Order.valid_date)
        ).all()
        by_date_status = {}
        for vd, st, n in status_breakdown:
            by_date_status.setdefault(vd, {})[st] = n

        # 各 instance 最新 NAV
        instance_navs = []
        for st_row in session.execute(select(InstanceState)).scalars().all():
            if configured is not None and st_row.instance_id not in configured:
                continue
            instance_cfg = (configured or {}).get(st_row.instance_id, {})
            latest_perf = session.execute(
                select(PerfSnapshot)
                .where(PerfSnapshot.instance_id == st_row.instance_id)
                .order_by(desc(PerfSnapshot.date))
                .limit(1)
            ).scalar()
            instance_navs.append({
                "instance_id": st_row.instance_id,
                "display_name": instance_cfg.get("display_name", st_row.instance_id),
                "virtual_cash": st_row.virtual_cash,
                "holdings_count": len(st_row.virtual_positions or {}),
                "last_update": st_row.last_update,
                "latest_nav": latest_perf.nav if latest_perf else None,
                "latest_nav_date": latest_perf.date if latest_perf else None,
                "latest_daily_return": latest_perf.daily_return if latest_perf else None,
                "is_shadow": False,
                "orders_enabled": instance_cfg.get("orders_enabled", True),
            })

        for st_row in session.execute(select(ShadowInstanceState)).scalars().all():
            latest_perf = session.execute(
                select(ShadowNavSnapshot)
                .where(ShadowNavSnapshot.shadow_id == st_row.shadow_id)
                .order_by(desc(ShadowNavSnapshot.date))
                .limit(1)
            ).scalar_one_or_none()
            instance_navs.append({
                "instance_id": st_row.shadow_id,
                "virtual_cash": st_row.virtual_cash,
                "holdings_count": len(st_row.virtual_positions or {}),
                "last_update": st_row.last_update,
                "latest_nav": latest_perf.nav if latest_perf else None,
                "latest_nav_date": latest_perf.date if latest_perf else None,
                "latest_daily_return": latest_perf.daily_return if latest_perf else None,
                "is_shadow": True,
                "orders_enabled": False,
            })

    return APIResponse[dict](
        code=0, message="ok",
        data={
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "pred_status": pred_status,
            "blacklist": bl_summary,
            "pending_orders_total": pending_count,
            "orders_by_date_status_7d": by_date_status,
            "instances": instance_navs,
        },
    )


# ── 8. /admin/strategy-state : 实例的策略持久状态 ─────────────────────────
@router.get(
    "/strategy-state",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def strategy_state(
    instance_id: str | None = None,
    sf=Depends(get_session_factory),
):
    """看实例的 strategy_state JSON（V20H 的 last_rb_idx / equity_history 等）。"""
    with sf() as session:
        stmt = select(InstanceState)
        if instance_id:
            stmt = stmt.where(InstanceState.instance_id == instance_id)
        rows = session.execute(stmt).scalars().all()
        items = [
            {
                "instance_id": r.instance_id,
                "virtual_cash": r.virtual_cash,
                "holdings_count": len(r.virtual_positions or {}),
                "last_update": r.last_update,
                "strategy_state": r.strategy_state,
            }
            for r in rows
        ]
    return APIResponse[dict](
        code=0, message="ok",
        data={"items": items, "count": len(items)},
    )


# ── 10. /admin/metrics/summary : 量化绩效指标 ─────────────────────────────
@router.get(
    "/metrics/summary",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def metrics_summary(
    instance_id: str = "paper_v20h_v20h_v1_3",
    period: str = Query("all", pattern=r"^(7d|30d|90d|180d|1y|ytd|all)$"),
    metrics: MetricsService = Depends(get_metrics_service),
):
    """综合绩效：累计/年化收益、Sharpe、Sortino、MaxDD、Calmar、胜率、VaR 等。"""
    summary = metrics.summary(instance_id, period=period)
    return APIResponse[dict](
        code=0, message="ok",
        data={
            "instance_id": instance_id,
            "period": period,
            **summary.to_dict(),
        },
    )


# ── 11. /admin/metrics/drawdown : 回撤序列 ────────────────────────────────
@router.get(
    "/metrics/drawdown",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def metrics_drawdown(
    instance_id: str = "paper_v20h_v20h_v1_3",
    period: str = Query("all", pattern=r"^(7d|30d|90d|180d|1y|ytd|all)$"),
    metrics: MetricsService = Depends(get_metrics_service),
):
    """每日相对历史最高点的 drawdown，给前端画水下曲线用。"""
    data = metrics.drawdown_series(instance_id, period=period)
    return APIResponse[dict](
        code=0, message="ok",
        data={
            "instance_id": instance_id,
            "period": period,
            **data,
        },
    )


# ── 12. /admin/metrics/periodic : 按周/月/年聚合收益 ──────────────────────
@router.get(
    "/metrics/periodic",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def metrics_periodic(
    instance_id: str = "paper_v20h_v20h_v1_3",
    period: str = Query("all", pattern=r"^(7d|30d|90d|180d|1y|ytd|all)$"),
    freq: str = Query("monthly", pattern=r"^(weekly|monthly|yearly)$"),
    metrics: MetricsService = Depends(get_metrics_service),
):
    """周度/月度/年度收益聚合表。"""
    items = metrics.periodic_returns(instance_id, period=period, freq=freq)
    return APIResponse[dict](
        code=0, message="ok",
        data={
            "instance_id": instance_id,
            "period": period,
            "freq": freq,
            "items": items,
            "count": len(items),
        },
    )


# ── 13. /admin/metrics/trade-analytics : 订单 + 成交统计 ─────────────────
@router.get(
    "/metrics/trade-analytics",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def metrics_trade_analytics(
    account_group: str | None = None,
    period: str = Query("30d", pattern=r"^(7d|30d|90d|180d|1y|ytd|all)$"),
    metrics: MetricsService = Depends(get_metrics_service),
):
    """订单 status 矩阵、fill_rate、累计 commission、bookkeeping_divergence 等。"""
    cutoff = date_range_for_period(period)
    data = metrics.trade_analytics(account_group=account_group, cutoff=cutoff)
    return APIResponse[dict](
        code=0, message="ok",
        data={"period": period, **data},
    )


# ── 9. /admin/bookkeeping-divergence : 真账户与虚拟账本对账分叉 ─────────
@router.get(
    "/bookkeeping-divergence",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def bookkeeping_divergence(
    sf=Depends(get_session_factory),
    limit: int = Query(200, ge=1, le=10000),
):
    """列出所有 bookkeeping_divergence=True 的 orders。

    这些订单 QMT 真账户已 FILL，但虚拟账本（instance_state）因防穿仓/超卖跳过了
    更新。需要人工对账：检查 QMT 真账户的现金 + 持仓，对齐 instance_state。
    """
    with sf() as session:
        stmt = (
            select(Order)
            .where(Order.bookkeeping_divergence == True)  # noqa: E712
            .order_by(desc(Order.valid_date), Order.symbol)
            .limit(limit)
        )
        rows = session.execute(stmt).scalars().all()
        items = [
            {
                "order_id": o.order_id,
                "valid_date": o.valid_date,
                "account_group": o.account_group,
                "symbol": o.symbol,
                "direction": o.direction,
                "quantity": o.quantity,
                "limit_price": o.limit_price,
                "status": o.status,
            }
            for o in rows
        ]
    return APIResponse[dict](
        code=0, message="ok",
        data={"items": items, "count": len(items)},
    )


# ── 14. POST /admin/reconcile-positions : 把 QMT 真实持仓推上来对账 ──────
@router.post(
    "/reconcile-positions",
    response_model=APIResponse[ReconcileResult],
)
async def reconcile_positions(
    snapshot: QmtPositionSnapshot,
    auth: AuthContext = Depends(verify_api_key),
    settings: Settings = Depends(get_settings),
    service: ReconcileService = Depends(get_reconcile_service),
):
    """对账：server virtual_positions vs QMT 真实持仓。

    Body:
      {
        "instance_id": "paper_v20h_v20h_v1_3",
        "qmt_account_id": "1234567890",
        "qmt_cash": 922928.0,
        "qmt_positions": {"600519.SH": 100, ...},
        "snapshot_time": "2026-05-21T14:30:00+08:00",
        "dry_run": true  // 默认 true，false 才真改 instance_state
      }

    dry_run=true: 返回 diff 详情供人眼审查
    dry_run=false: 强制把 instance_state.virtual_cash/positions 改成 QMT 状态
    """
    # 少数纯单元测试会直接调用 endpoint 函数而绕过 FastAPI DI；生产 HTTP
    # 始终注入 AuthContext。直接调用按 legacy paper 上下文处理以保持兼容。
    if not isinstance(auth, AuthContext):
        auth = AuthContext(
            execution_domain="paper",
            client_id="direct-call-legacy",
            allowed_account_aliases=(),
        )
    if snapshot.execution_domain != auth.execution_domain:
        raise HTTPException(status_code=403, detail="reconcile execution_domain 跨域")
    if not auth.allows_account(snapshot.account_alias):
        raise HTTPException(status_code=403, detail="token 无权访问该 account_alias")
    if auth.execution_domain == "live":
        fingerprint = hashlib.sha256(snapshot.qmt_account_id.encode()).hexdigest()
        if (
            not settings.live_qmt_account_sha256
            or fingerprint != settings.live_qmt_account_sha256
        ):
            raise HTTPException(status_code=403, detail="QMT account fingerprint 不匹配")
        if not snapshot.dry_run or snapshot.force:
            raise HTTPException(
                status_code=403,
                detail="live token 仅允许只读对账；账本修复必须走受控恢复流程",
            )

    # Portfolio total reconciliation must run before any per-instance operation.
    # This still records the authoritative total diff when a shared-account apply
    # is correctly rejected below.
    # snapshot.qmt_positions 是全账户持仓（client 每个 instance 都推同一份），
    # 故此处即可做 portfolio 级 Σ台账 vs QMT 校验。
    try:
        service.shadow_compare(
            snapshot.qmt_positions, snapshot.qmt_cash, snapshot.snapshot_time,
            execution_domain=snapshot.execution_domain,
        )
    except Exception:
        logger.exception("shadow_compare failed (non-fatal, ignored)")

    try:
        result = service.reconcile(snapshot)
    except InstanceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ReconcileSanityCheckFailed as e:
        # 返回 422 而非 500，告诉 client 是数据校验问题而非系统故障
        raise HTTPException(status_code=422, detail=str(e))

    return APIResponse[ReconcileResult](code=0, message="ok", data=result)


# ── 15. /admin/heartbeat : client 活动心跳检查 ─────────────────────────────
@router.get(
    "/heartbeat",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def heartbeat(
    sf=Depends(get_session_factory),
):
    """检查 client 端是否按时跑了每日流程。返回告警列表 + 各步骤时间戳。

    每天 client 应该做的事：
      09:10  order_submit.py     → GET /orders?date=T  (T = 今天)
      15:10  trade_result_push   → POST /trade-result
      15:30  data_collector      → POST /market-data
      16:00  trigger_pipeline    → POST /admin/run-pipeline?trade_date=T+1

    通过 server 数据库追溯每一步的实际发生时间：
      - GET /orders 看 raw_signals 表（生成信号的时间）
      - trade_result 看 trades 表 received_at
      - market-data 看 stocks parquet 的最新 trade_date
      - run-pipeline 看 raw_signals 表的 signal_time (T+1 valid_date)

    适合外部 cron / UptimeRobot 调用做自动监控：
        curl -H "Authorization: Bearer $KEY" .../admin/heartbeat
    """
    from app.models import RawSignal, Trade

    now = datetime.now()
    today = now.strftime("%Y%m%d")
    today_dt = now.strftime("%Y-%m-%d")
    is_trading_day_proxy = now.isoweekday() <= 5  # 简化：周一到周五；不考虑节假日

    alerts: list[str] = []
    timeline = {}

    with sf() as session:
        # 1. 今天有没有 trigger pipeline (从 raw_signals 表看 signal_time)
        last_signal = session.execute(
            select(RawSignal.signal_time, RawSignal.valid_date)
            .order_by(desc(RawSignal.signal_time))
            .limit(1)
        ).first()
        if last_signal:
            timeline["last_signal_time"] = last_signal[0]
            timeline["last_signal_valid_date"] = last_signal[1]
        else:
            timeline["last_signal_time"] = None

        # 2. 今天有没有 trade_result 推回（trades 表 received_at）
        last_trade = session.execute(
            select(Trade.received_at)
            .order_by(desc(Trade.received_at))
            .limit(1)
        ).first()
        timeline["last_trade_result"] = last_trade[0] if last_trade else None

        # 3. 今天有没有为今天 trigger 过 (信号 valid_date 包含今天 OR 明天的)
        today_or_future_signals = session.execute(
            select(func.count())
            .select_from(RawSignal)
            .where(RawSignal.valid_date >= today)
        ).scalar()
        timeline["pending_signals_for_today_or_later"] = today_or_future_signals

    # 4. 看 market_data 最新 trade_date（从 stocks parquet）
    latest_market_date = None
    stocks_dir = Path("/opt/qmt-server/v2.3/server/data/market/daily/stocks")
    if not stocks_dir.exists():
        stocks_dir = Path("./data/market/daily/stocks")  # local dev fallback
    if stocks_dir.exists():
        # 随便挑一只票看最新日期（用一个高频活跃股票）
        for stem in ["000001.SZ", "600000.SH", "000002.SZ"]:
            fp = stocks_dir / f"{stem}.parquet"
            if fp.exists():
                try:
                    import pandas as pd
                    df = pd.read_parquet(fp, columns=["trade_date"])
                    if not df.empty:
                        latest_market_date = str(int(df["trade_date"].max()))
                        break
                except Exception:
                    pass
    timeline["latest_market_data_date"] = latest_market_date

    # 5. 判断告警
    if is_trading_day_proxy:
        # 9:30 之后应该看到当天 trigger（或者前一天 16:00 trigger 后的当天 signal valid_date）
        if now.hour >= 10 and now.hour < 15:
            # 今天 9-15 点期间，应该已经有当天的 PENDING 订单
            # （或者至少昨晚 trigger 应该把今天的 valid_date 信号写进来）
            if timeline.get("pending_signals_for_today_or_later", 0) == 0:
                alerts.append(
                    f"⚠ {today_dt} 09:00 之后 没有任何 valid_date >= {today} 的 raw_signals，"
                    f"是不是 partner 昨晚没 trigger / 今早 client 没启动？"
                )

        # 15:30 之后应该看到当天的 OHLCV 推送
        if now.hour >= 16 and latest_market_date:
            if int(latest_market_date) < int(today):
                alerts.append(
                    f"⚠ {today_dt} 16:00 之后 OHLCV 最新仍是 {latest_market_date}，"
                    f"partner 还没推今天的行情数据"
                )

        # 16:00 之后应该看到 trade_result（如果有委托）
        if now.hour >= 17:
            last_tr = timeline.get("last_trade_result")
            if last_tr and last_tr[:10] != today_dt:
                alerts.append(
                    f"⚠ {today_dt} 17:00 之后 trade_result 最新仍是 {last_tr[:10]}，"
                    f"partner 还没推今天的成交回报"
                )

    return APIResponse[dict](
        code=0, message="ok",
        data={
            "now": now.isoformat(timespec="seconds"),
            "today": today,
            "is_trading_day_proxy": is_trading_day_proxy,
            "timeline": timeline,
            "alerts": alerts,
            "status": "alert" if alerts else "ok",
        },
    )
