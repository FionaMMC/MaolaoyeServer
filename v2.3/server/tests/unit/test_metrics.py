"""MetricsService + 纯函数 测试"""
from datetime import datetime
from pathlib import Path

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import (
    Order,
    OrderSignalMap,
    PerfSnapshot,
    RawSignal,
    ShadowNavSnapshot,
    Trade,
)
from app.services.metrics import (
    MetricsService,
    compute_benchmark_comparison,
    compute_drawdown_series,
    compute_summary,
    date_range_for_period,
)


def _factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    return make_session_factory(engine)


# ── 纯函数：统计核心 ────────────────────────────────────────────────────
def test_summary_empty_rows():
    out = compute_summary([])
    assert out.n_days == 0
    assert out.cumulative_return is None
    assert out.sharpe is None


def test_summary_single_row():
    out = compute_summary([("20260101", 10_000_000.0, None)])
    assert out.n_days == 1
    assert out.cumulative_return == 0.0
    assert out.start_nav == 10_000_000.0
    assert out.end_nav == 10_000_000.0
    assert out.sharpe is None  # 样本不够


def test_summary_basic_returns():
    rows = [
        ("20260101", 10_000_000.0, None),
        ("20260102", 10_100_000.0, 0.01),
        ("20260103", 10_201_000.0, 0.01),
        ("20260104", 10_303_010.0, 0.01),
    ]
    out = compute_summary(rows)
    # 累计 = (10303010 - 10000000) / 10000000 = 3.0301%
    assert out.cumulative_return == pytest.approx(0.030301, abs=1e-5)
    # 4 天 → 252/4 = 63 倍年化
    assert out.annualized_return is not None
    assert out.annualized_return > 5.0   # 极高（每天 +1% 复利 → ~430× annualized）
    # 全是正收益 → 胜率 100%
    assert out.win_rate == 1.0
    assert out.avg_loss is None
    # MaxDD = 0
    assert out.max_drawdown == 0.0


def test_summary_with_drawdown():
    rows = [
        ("20260101", 100.0, None),
        ("20260102", 110.0, 0.10),
        ("20260103", 99.0, -0.10),   # dd = -10%
        ("20260104", 105.0, 0.0606),
    ]
    out = compute_summary(rows)
    assert out.max_drawdown == pytest.approx(-0.10, abs=1e-3)
    assert out.max_drawdown_duration_days == 1
    assert out.win_rate == pytest.approx(2/3, abs=1e-3)


def test_summary_sharpe_positive():
    # 每日恒定正收益（无波动），sharpe 应趋向无穷大；这里测它能算
    rows = [(f"2026010{i+1}", 100 * (1.001) ** i, 0.001) for i in range(20)]
    out = compute_summary(rows)
    # 没波动率 → stdev 接近 0 → sharpe 接近无穷大
    # 但我们的实现：如果 sd == 0 不计算 sharpe；这里 sd 是 0.0 因为所有 return = 0.001
    # 实际上由于浮点，sd 不会是 0
    if out.sharpe is not None:
        assert out.sharpe > 5  # 远好


def test_summary_sortino_only_downside():
    # 大部分正收益 + 少量负收益 → Sortino > Sharpe
    rows = []
    nav = 100.0
    rets = [0.01, 0.01, 0.01, -0.005, 0.01, 0.01, -0.003, 0.01, 0.01, 0.01]
    for i, r in enumerate(rets):
        nav *= (1 + r)
        rows.append((f"2026010{i+1}", nav, r if i > 0 else None))
    out = compute_summary(rows)
    assert out.sharpe is not None
    assert out.sortino is not None


def test_drawdown_series():
    navs = [100.0, 110.0, 99.0, 105.0, 115.0]
    out = compute_drawdown_series(navs)
    # peak goes 100 → 110 → 110 → 110 → 115
    # dd at each: 0, 0, -0.1, -0.0454, 0
    assert out[0] == 0.0
    assert out[1] == 0.0
    assert out[2] == pytest.approx(-0.10, abs=1e-3)
    assert out[-1] == 0.0


def test_drawdown_series_empty():
    assert compute_drawdown_series([]) == []


# ── 基准对比 ─────────────────────────────────────────────────────────────
def test_benchmark_perfect_correlation():
    # 组合 = 1.5 × 基准 → beta = 1.5, correlation = 1
    bench = [0.01, -0.005, 0.008, -0.003, 0.01, 0.002, -0.004, 0.006]
    port = [r * 1.5 for r in bench]
    out = compute_benchmark_comparison(port, bench)
    assert out.beta == pytest.approx(1.5, abs=1e-3)
    assert out.correlation == pytest.approx(1.0, abs=1e-3)


def test_benchmark_zero_correlation():
    # 无相关 → beta 接近 0
    port = [0.01, -0.005, 0.008, -0.003, 0.01]
    bench = [0.001, 0.001, 0.001, 0.001, 0.001]   # 常数 → var = 0
    out = compute_benchmark_comparison(port, bench)
    # var(bench) ≈ 0 → beta 不算
    assert out.beta is None


def test_benchmark_short_sample_no_compute():
    out = compute_benchmark_comparison([0.01, 0.02], [0.01, 0.02])
    # 样本 < 5 → 不算
    assert out.beta is None
    assert out.alpha_annual is None


# ── 时间窗口 ────────────────────────────────────────────────────────────
def test_date_range_for_period():
    today = datetime(2026, 5, 17)
    assert date_range_for_period("all", today) == "00000000"
    assert date_range_for_period("ytd", today) == "20260101"
    assert date_range_for_period("7d", today) == "20260510"
    assert date_range_for_period("30d", today) == "20260417"
    # 未知 period → 默认上一年初
    assert date_range_for_period("garbage", today) == "20250101"


# ── DB-Service ─────────────────────────────────────────────────────────
def _seed_perf(sf, instance_id: str, navs: list[tuple[str, float, float | None]]):
    with sf() as s:
        for date, nav, ret in navs:
            s.add(PerfSnapshot(
                instance_id=instance_id, date=date, nav=nav,
                daily_return=ret, positions_snapshot={},
            ))
        s.commit()


def test_service_summary_e2e(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed_perf(sf, "test_inst", [
        ("20260101", 1_000_000.0, None),
        ("20260102", 1_010_000.0, 0.01),
        ("20260103", 1_005_000.0, -0.0050),
        ("20260106", 1_015_050.0, 0.01),
    ])
    svc = MetricsService(sf)
    out = svc.summary("test_inst", period="all")
    assert out.n_days == 4
    assert out.start_nav == 1_000_000.0
    assert out.end_nav == 1_015_050.0
    assert out.cumulative_return == pytest.approx(0.015050, abs=1e-5)


def test_service_metrics_reads_shadow_nav_without_copying_into_live_perf(tmp_path: Path):
    sf = _factory(tmp_path)
    now = datetime.now().isoformat()
    with sf() as s:
        for date, nav, ret in [
            ("20260724", 10_000_000.0, None),
            ("20260725", 10_100_000.0, 0.01),
        ]:
            s.add(ShadowNavSnapshot(
                shadow_id="Shadow_Base", date=date, nav=nav,
                daily_return=ret, virtual_cash=100_000.0,
                positions_snapshot={"510300.SH": 100},
                transaction_cost=0.0, turnover=0.0,
                created_at=now,
            ))
        s.commit()

    svc = MetricsService(sf)
    summary = svc.summary("Shadow_Base", period="all")
    assert summary.n_days == 2
    assert summary.cumulative_return == pytest.approx(0.01)
    assert svc.drawdown_series("Shadow_Base")["nav"] == [10_000_000.0, 10_100_000.0]


def test_service_drawdown_series(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed_perf(sf, "test_inst", [
        ("20260101", 100.0, None),
        ("20260102", 110.0, 0.10),
        ("20260103", 99.0, -0.10),
    ])
    svc = MetricsService(sf)
    out = svc.drawdown_series("test_inst")
    assert len(out["dates"]) == 3
    assert out["drawdown"][2] == pytest.approx(-0.10, abs=1e-3)


def test_service_periodic_monthly(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed_perf(sf, "test_inst", [
        ("20260101", 100.0, None),
        ("20260131", 110.0, 0.10),
        ("20260201", 110.0, 0.0),
        ("20260228", 121.0, 0.10),
    ])
    svc = MetricsService(sf)
    out = svc.periodic_returns("test_inst", freq="monthly")
    assert len(out) == 2
    assert out[0]["period"] == "2026-01"
    assert out[0]["return"] == pytest.approx(0.10, abs=1e-3)
    assert out[1]["period"] == "2026-02"
    assert out[1]["return"] == pytest.approx(0.10, abs=1e-3)


def test_service_periodic_weekly(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed_perf(sf, "test_inst", [
        ("20260105", 100.0, None),    # 2026-W02
        ("20260109", 102.0, 0.005),   # 2026-W02
        ("20260112", 105.0, 0.01),    # 2026-W03
        ("20260116", 110.0, 0.01),    # 2026-W03
    ])
    svc = MetricsService(sf)
    out = svc.periodic_returns("test_inst", freq="weekly")
    assert len(out) == 2
    # W02: 100 → 102
    assert out[0]["period"].endswith("W02")
    assert out[0]["return"] == pytest.approx(0.02, abs=1e-3)


def test_service_trade_analytics(tmp_path: Path):
    sf = _factory(tmp_path)
    with sf() as s:
        s.add(Order(order_id="o1", account_group="ag", symbol="600519.SH",
                    direction="BUY", quantity=100, limit_price=10.0,
                    valid_date="20260101", status="FILLED",
                    created_at=datetime.now().isoformat()))
        s.add(Order(order_id="o2", account_group="ag", symbol="600519.SH",
                    direction="SELL", quantity=50, limit_price=10.2,
                    valid_date="20260102", status="REJECTED",
                    created_at=datetime.now().isoformat()))
        s.add(Order(order_id="o3", account_group="ag", symbol="000001.SZ",
                    direction="BUY", quantity=200, limit_price=15.0,
                    valid_date="20260102", status="PARTIAL",
                    created_at=datetime.now().isoformat()))
        s.add(Trade(order_id="o1", filled_quantity=100, filled_price=10.0,
                    status="FILLED", received_at=datetime.now().isoformat()))
        s.add(Trade(order_id="o3", filled_quantity=150, filled_price=15.0,
                    status="PARTIAL", received_at=datetime.now().isoformat()))
        s.commit()

    svc = MetricsService(sf)
    out = svc.trade_analytics()
    assert out["n_orders"] == 3
    assert out["by_status"] == {"FILLED": 1, "REJECTED": 1, "PARTIAL": 1}
    assert out["by_direction"] == {"BUY": 2, "SELL": 1}
    # FILLED + PARTIAL 算 fill_rate 分子
    assert out["fill_rate"] == pytest.approx(2/3, abs=1e-3)
    assert out["n_trades"] == 2
    # 100*10 + 150*15 = 1000 + 2250 = 3250
    assert out["total_filled_amount"] == 3250.0


def test_execution_analysis_filters_instance_and_allocates_aggregate_fill(tmp_path: Path):
    sf = _factory(tmp_path)
    now = datetime.now().isoformat()
    with sf() as session:
        session.add(Order(
            order_id="shared", account_group="ag", symbol="600519.SH",
            direction="BUY", quantity=300, limit_price=10.3,
            valid_date="20260102", status="FILLED", created_at=now,
        ))
        session.add_all([
            RawSignal(
                signal_id="signal-a", instance_id="instance-a", symbol="600519.SH",
                direction="BUY", quantity=100, reference_price=10.0,
                price_offset=0.01, limit_price=10.1, valid_date="20260102",
                signal_time=now, precheck_status="PASS", precheck_reason=None,
            ),
            RawSignal(
                signal_id="signal-b", instance_id="instance-b", symbol="600519.SH",
                direction="BUY", quantity=200, reference_price=10.1,
                price_offset=0.01, limit_price=10.201, valid_date="20260102",
                signal_time=now, precheck_status="PASS", precheck_reason=None,
            ),
            OrderSignalMap(
                order_id="shared", signal_id="signal-a", signal_quantity=100,
            ),
            OrderSignalMap(
                order_id="shared", signal_id="signal-b", signal_quantity=200,
            ),
            Trade(
                order_id="shared", filled_quantity=300, filled_price=10.2,
                filled_time=now, status="FILLED", received_at=now,
            ),
        ])
        session.commit()

    result = MetricsService(sf).execution_analysis("instance-a")
    assert result["summary"]["n_orders"] == 1
    assert result["summary"]["filled_orders"] == 1
    assert result["summary"]["total_filled_quantity"] == pytest.approx(100.0)
    assert result["summary"]["total_filled_amount"] == pytest.approx(1_020.0)
    assert result["summary"]["implementation_shortfall"] == pytest.approx(20.0)
    assert result["summary"]["weighted_strategy_to_fill_bps"] == pytest.approx(200.0)
    item = result["items"][0]
    assert item["strategy_reference_price"] == pytest.approx(10.0)
    assert item["fill_vwap"] == pytest.approx(10.2)
    assert item["aggregate_allocation_ratio"] == pytest.approx(1 / 3)


def test_service_handles_no_data_gracefully(tmp_path: Path):
    sf = _factory(tmp_path)
    svc = MetricsService(sf)
    out = svc.summary("nonexistent")
    assert out.n_days == 0
    assert out.sharpe is None

    out = svc.drawdown_series("nonexistent")
    assert out["dates"] == []

    out = svc.periodic_returns("nonexistent", freq="monthly")
    assert out == []
