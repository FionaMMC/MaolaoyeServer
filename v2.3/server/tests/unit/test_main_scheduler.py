"""APScheduler 在 lifespan 中按 settings 启停。

根因修复：之前 main.py 只 import make_scheduler 不启动 → 没有任何东西每天 16:00
触发策略管线。这里验证 scheduler_enabled 时 lifespan 会真正启动 cron job。
"""
from pathlib import Path

from fastapi.testclient import TestClient

from app.dependencies import _engine_for_url
from app.main import create_app
from app.scheduler.runtime import make_scheduler
from app.settings import Settings, get_settings


def _settings(tmp_path: Path, **kw) -> Settings:
    get_settings.cache_clear()
    _engine_for_url.cache_clear()
    s = Settings(
        api_key="K",
        db_url=f"sqlite:///{tmp_path}/t.db",
        parquet_root=tmp_path / "data",
        plugins_dir=tmp_path / "plugins",
        strategies_file=tmp_path / "strategies.yaml",
        log_level="WARNING",
        **kw,
    )
    s.parquet_root.mkdir(parents=True, exist_ok=True)
    s.plugins_dir.mkdir(parents=True, exist_ok=True)
    return s


def test_scheduler_off_by_default(tmp_path):
    app = create_app(settings_override=_settings(tmp_path))
    with TestClient(app):
        assert getattr(app.state, "scheduler", None) is None


def test_scheduler_starts_when_enabled(tmp_path):
    app = create_app(settings_override=_settings(
        tmp_path, scheduler_enabled=True, scheduler_cron_hour=16, scheduler_cron_minute=0))
    with TestClient(app):
        sched = getattr(app.state, "scheduler", None)
        assert sched is not None
        assert sched.running
        job = sched.get_job("strategy_pipeline_daily")
        assert job is not None
    # 退出 context 后应已 shutdown（不抛异常即可）


def test_shadow_job_runs_even_when_order_pipeline_fails():
    calls = []

    def failed_pipeline(trade_date):
        calls.append(("pipeline", trade_date))
        raise RuntimeError("order pipeline failed")

    def shadow_run(trade_date):
        calls.append(("shadow", trade_date))
        return {"instances": []}

    scheduler = make_scheduler(failed_pipeline, shadow_run=shadow_run)
    scheduler.get_job("strategy_pipeline_daily").func()

    assert [name for name, _ in calls] == ["pipeline", "shadow"]
