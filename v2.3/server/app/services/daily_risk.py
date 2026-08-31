"""Build and query durable end-of-day portfolio risk snapshots."""
from __future__ import annotations

import json
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from app.models import (
    CashFlowJournal,
    DailyRiskSnapshot,
    PerfSnapshot,
    ShadowNavSnapshot,
)
from app.services.metrics import compute_benchmark_comparison, date_range_for_period

CALCULATION_VERSION = "daily-risk-v1"
BENCHMARK_NAMES = {
    "000300.SH": "CSI 300",
    "000852.SH": "CSI 1000",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _positions(raw) -> dict[str, float]:
    payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return {
        str(symbol): float(quantity)
        for symbol, quantity in payload.items()
        if float(quantity) != 0
    }


def _compound(returns: list[float]) -> float | None:
    if not returns:
        return None
    value = 1.0
    for item in returns:
        value *= 1.0 + item
    return value - 1.0


class DailyRiskSnapshotService:
    """Materialize portfolio facts once, then join benchmarks at read time."""

    def __init__(self, session_factory, parquet_store):
        self.sf = session_factory
        self.store = parquet_store
        self._price_cache: dict[str, tuple[list[int], list[float], str]] = {}

    def _price_series(self, symbol: str) -> tuple[list[int], list[float], str]:
        cached = self._price_cache.get(symbol)
        if cached is not None:
            return cached
        category = "stocks"
        frame = self.store.read(category, symbol)
        if frame.empty:
            category = "etfs"
            frame = self.store.read(category, symbol)
        if frame.empty or "close" not in frame.columns:
            result = ([], [], "missing")
        else:
            clean = frame[["trade_date", "close"]].dropna().sort_values("trade_date")
            result = (
                [int(value) for value in clean["trade_date"]],
                [float(value) for value in clean["close"]],
                category,
            )
        self._price_cache[symbol] = result
        return result

    def _mark(self, symbol: str, date: int) -> tuple[float, int, str] | None:
        dates, closes, category = self._price_series(symbol)
        offset = bisect_right(dates, date) - 1
        if offset < 0:
            return None
        return closes[offset], dates[offset], category

    def rebuild(
        self,
        *,
        instance_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        execution_domain: str | None = None,
    ) -> dict:
        """Idempotently rebuild materialized snapshots from authoritative facts."""
        with self.sf() as session:
            regular = session.execute(
                select(PerfSnapshot).order_by(PerfSnapshot.instance_id, PerfSnapshot.date)
            ).scalars().all()
            shadow = session.execute(
                select(ShadowNavSnapshot).order_by(
                    ShadowNavSnapshot.shadow_id, ShadowNavSnapshot.date,
                )
            ).scalars().all()
            flows = session.execute(
                select(CashFlowJournal).where(CashFlowJournal.status == "APPLIED")
            ).scalars().all()

        flow_totals: dict[tuple[str, str, str], list[float | int]] = defaultdict(
            lambda: [0.0, 0]
        )
        for flow in flows:
            key = (flow.execution_domain, flow.instance_id, flow.event_date)
            flow_totals[key][0] += float(flow.amount)
            flow_totals[key][1] += 1

        sources: list[dict] = []
        for row in regular:
            sources.append({
                "instance_id": row.instance_id,
                "date": row.date,
                "execution_domain": row.execution_domain,
                "instance_kind": "regular",
                "nav": float(row.nav),
                "cash": None,
                "positions": _positions(row.positions_snapshot),
            })
        for row in shadow:
            sources.append({
                "instance_id": row.shadow_id,
                "date": row.date,
                "execution_domain": "paper",
                "instance_kind": "shadow",
                "nav": float(row.nav),
                "cash": float(row.virtual_cash),
                "positions": _positions(row.positions_snapshot),
            })
        sources.sort(key=lambda item: (item["instance_id"], item["date"]))

        previous_nav: dict[str, float] = {}
        written = 0
        missing_marks = 0
        stale_marks = 0
        now = _now_iso()
        with self.sf() as session:
            for source in sources:
                source_instance = source["instance_id"]
                source_date = source["date"]
                prior_nav = previous_nav.get(source_instance)
                previous_nav[source_instance] = source["nav"]
                if instance_id and source_instance != instance_id:
                    continue
                if execution_domain and source["execution_domain"] != execution_domain:
                    continue
                if start_date and source_date < start_date:
                    continue
                if end_date and source_date > end_date:
                    continue

                positions = source["positions"]
                long_value = 0.0
                short_value = 0.0
                priced = 0
                stale = 0
                missing = 0
                marked_positions = []
                for symbol, quantity in positions.items():
                    mark = self._mark(symbol, int(source_date))
                    if mark is None:
                        missing += 1
                        continue
                    close, mark_date, category = mark
                    priced += 1
                    stale += int(mark_date != int(source_date))
                    market_value = quantity * close
                    if market_value >= 0:
                        long_value += market_value
                    else:
                        short_value += abs(market_value)
                    marked_positions.append({
                        "symbol": symbol,
                        "quantity": quantity,
                        "mark_price": close,
                        "mark_date": str(mark_date),
                        "market_value": market_value,
                        "category": category,
                    })

                gross_value = long_value + short_value
                net_value = long_value - short_value
                nav = source["nav"]
                if source["cash"] is None:
                    cash = nav - net_value
                    cash_source = "nav_residual" if missing == 0 else "nav_residual_incomplete"
                else:
                    cash = source["cash"]
                    cash_source = "snapshot"
                holding_count = len(positions)
                coverage = priced / holding_count if holding_count else 1.0
                flow_key = (source["execution_domain"], source_instance, source_date)
                flow_amount, flow_count = flow_totals.get(flow_key, [0.0, 0])
                if source["instance_kind"] == "shadow":
                    cash_flow_status = "not_applicable_shadow"
                elif flow_count:
                    cash_flow_status = "observed"
                elif source["execution_domain"] == "paper":
                    cash_flow_status = "assumed_zero_paper"
                else:
                    cash_flow_status = "missing_live"
                portfolio_return = None
                if prior_nav not in (None, 0):
                    portfolio_return = (nav - float(flow_amount)) / prior_nav - 1.0

                for item in marked_positions:
                    item["weight"] = item["market_value"] / nav if nav else None
                marked_positions.sort(
                    key=lambda item: abs(item["market_value"]), reverse=True,
                )
                values = {
                    "execution_domain": source["execution_domain"],
                    "instance_kind": source["instance_kind"],
                    "nav": nav,
                    "cash": cash,
                    "cash_source": cash_source,
                    "long_market_value": long_value,
                    "short_market_value": short_value,
                    "gross_market_value": gross_value,
                    "net_market_value": net_value,
                    "gross_exposure": gross_value / nav if nav else None,
                    "net_exposure": net_value / nav if nav else None,
                    "cash_ratio": cash / nav if nav else None,
                    "holdings_count": holding_count,
                    "priced_holdings_count": priced,
                    "stale_mark_count": stale,
                    "missing_mark_count": missing,
                    "pricing_coverage": coverage,
                    "top_positions": marked_positions[:20],
                    "external_cash_flow": float(flow_amount),
                    "cash_flow_status": cash_flow_status,
                    "portfolio_return": portfolio_return,
                    "calculation_version": CALCULATION_VERSION,
                    "updated_at": now,
                }
                row = session.get(DailyRiskSnapshot, (source_instance, source_date))
                if row is None:
                    row = DailyRiskSnapshot(
                        instance_id=source_instance,
                        date=source_date,
                        created_at=now,
                        **values,
                    )
                    session.add(row)
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
                written += 1
                missing_marks += missing
                stale_marks += stale
            session.commit()
        return {
            "written": written,
            "missing_marks": missing_marks,
            "stale_marks": stale_marks,
            "calculation_version": CALCULATION_VERSION,
        }

    def upsert_for_date(
        self, date: str | int, execution_domain: str | None = None,
    ) -> dict:
        date_str = str(date)
        return self.rebuild(
            start_date=date_str,
            end_date=date_str,
            execution_domain=execution_domain,
        )

    @staticmethod
    def _serialize(row: DailyRiskSnapshot) -> dict:
        return {
            "date": row.date,
            "nav": float(row.nav),
            "cash": float(row.cash),
            "cash_source": row.cash_source,
            "long_market_value": float(row.long_market_value),
            "short_market_value": float(row.short_market_value),
            "gross_market_value": float(row.gross_market_value),
            "net_market_value": float(row.net_market_value),
            "gross_exposure": row.gross_exposure,
            "net_exposure": row.net_exposure,
            "cash_ratio": row.cash_ratio,
            "holdings_count": row.holdings_count,
            "priced_holdings_count": row.priced_holdings_count,
            "stale_mark_count": row.stale_mark_count,
            "missing_mark_count": row.missing_mark_count,
            "pricing_coverage": row.pricing_coverage,
            "external_cash_flow": row.external_cash_flow,
            "cash_flow_status": row.cash_flow_status,
            "portfolio_return": row.portfolio_return,
            "calculation_version": row.calculation_version,
        }

    def query(
        self,
        instance_id: str,
        period: str = "all",
        benchmark_symbol: str = "000852.SH",
    ) -> dict:
        cutoff = date_range_for_period(period)
        with self.sf() as session:
            rows = session.execute(
                select(DailyRiskSnapshot)
                .where(DailyRiskSnapshot.instance_id == instance_id)
                .where(DailyRiskSnapshot.date >= cutoff)
                .order_by(DailyRiskSnapshot.date)
            ).scalars().all()

        benchmark = self.store.read("indexes", benchmark_symbol)
        benchmark_map: dict[str, tuple[float, float | None]] = {}
        if not benchmark.empty and {"trade_date", "close"}.issubset(benchmark.columns):
            clean = benchmark[["trade_date", "close"]].dropna().sort_values("trade_date")
            previous_close = None
            for record in clean.itertuples(index=False):
                close = float(record.close)
                daily_return = None if previous_close in (None, 0) else close / previous_close - 1.0
                benchmark_map[str(int(record.trade_date))] = (close, daily_return)
                previous_close = close

        items = [self._serialize(row) for row in rows]
        portfolio_growth = 1.0
        benchmark_base = None
        aligned_portfolio_returns = []
        aligned_benchmark_returns = []
        for index, item in enumerate(items):
            benchmark_close, benchmark_return = benchmark_map.get(item["date"], (None, None))
            if index == 0:
                item["portfolio_cumulative_return"] = 0.0
            else:
                if item["portfolio_return"] is not None:
                    portfolio_growth *= 1.0 + float(item["portfolio_return"])
                item["portfolio_cumulative_return"] = portfolio_growth - 1.0
            if benchmark_close is not None and benchmark_base is None:
                benchmark_base = benchmark_close
            item["benchmark_close"] = benchmark_close
            item["benchmark_return"] = benchmark_return
            item["benchmark_cumulative_return"] = (
                benchmark_close / benchmark_base - 1.0
                if benchmark_close is not None and benchmark_base not in (None, 0)
                else None
            )
            item["excess_cumulative_return"] = (
                item["portfolio_cumulative_return"] - item["benchmark_cumulative_return"]
                if item["benchmark_cumulative_return"] is not None else None
            )
            if index > 0 and item["portfolio_return"] is not None and benchmark_return is not None:
                aligned_portfolio_returns.append(float(item["portfolio_return"]))
                aligned_benchmark_returns.append(float(benchmark_return))

        comparison = compute_benchmark_comparison(
            aligned_portfolio_returns,
            aligned_benchmark_returns,
            benchmark_name=BENCHMARK_NAMES.get(benchmark_symbol, benchmark_symbol),
        ).to_dict()
        comparison.update({
            "portfolio_return": _compound(aligned_portfolio_returns),
            "benchmark_return": _compound(aligned_benchmark_returns),
        })
        comparison["excess_return"] = (
            comparison["portfolio_return"] - comparison["benchmark_return"]
            if comparison["portfolio_return"] is not None
            and comparison["benchmark_return"] is not None else None
        )
        latest = self._serialize(rows[-1]) if rows else None
        return {
            "instance_id": instance_id,
            "period": period,
            "benchmark": {
                "symbol": benchmark_symbol,
                "name": BENCHMARK_NAMES.get(benchmark_symbol, benchmark_symbol),
                "available": not benchmark.empty,
                "aligned_return_days": len(aligned_benchmark_returns),
            },
            "summary": {
                **comparison,
                "external_cash_flow": sum(item["external_cash_flow"] for item in items),
                "pricing_coverage": latest["pricing_coverage"] if latest else None,
                "latest": latest,
            },
            "latest_positions": rows[-1].top_positions if rows else [],
            "count": len(items),
            "items": items,
        }
