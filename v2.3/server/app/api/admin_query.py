"""Admin 只读查询端点：把 SQLite 关键表暴露成 HTTP API。

用户从 Mac 端 curl 就能拉到所有诊断数据，不需要 ssh + sqlite。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select

from app.auth import verify_api_key
from app.dependencies import (
    get_blacklist_service,
    get_data_upload_service,
    get_metrics_service,
    get_session_factory,
    get_settings,
)
from app.models import (
    InstanceState,
    Order,
    PerfSnapshot,
    RawSignal,
    Trade,
)
from app.schemas.common import APIResponse
from app.services.blacklist import BlacklistService
from app.services.data_upload import DataUploadService
from app.services.metrics import MetricsService, date_range_for_period
from app.settings import Settings

router = APIRouter(prefix="/admin")


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
    limit: int = Query(60, ge=1, le=1000),
    sf=Depends(get_session_factory),
):
    """返回 perf_snapshots 最近 N 个交易日的 NAV。可指定 instance_id。"""
    with sf() as session:
        stmt = select(PerfSnapshot).order_by(
            desc(PerfSnapshot.date), PerfSnapshot.instance_id,
        )
        if instance_id:
            stmt = stmt.where(PerfSnapshot.instance_id == instance_id)
        stmt = stmt.limit(limit)
        rows = session.execute(stmt).scalars().all()
        items = [
            {
                "instance_id": r.instance_id,
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
        data={"count": len(items), "items": items},
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
    today = datetime.now().strftime("%Y%m%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
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
            latest_perf = session.execute(
                select(PerfSnapshot)
                .where(PerfSnapshot.instance_id == st_row.instance_id)
                .order_by(desc(PerfSnapshot.date))
                .limit(1)
            ).scalar()
            instance_navs.append({
                "instance_id": st_row.instance_id,
                "virtual_cash": st_row.virtual_cash,
                "holdings_count": len(st_row.virtual_positions or {}),
                "last_update": st_row.last_update,
                "latest_nav": latest_perf.nav if latest_perf else None,
                "latest_nav_date": latest_perf.date if latest_perf else None,
                "latest_daily_return": latest_perf.daily_return if latest_perf else None,
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
