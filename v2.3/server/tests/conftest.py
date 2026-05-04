"""tests/conftest.py — 共享 fixtures"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings, get_settings


@pytest.fixture
def settings_for_test(tmp_path: Path) -> Settings:
    """每个测试一个隔离的 Settings（tmp 路径）。"""
    s = Settings(
        api_key="TEST_KEY",
        db_url=f"sqlite:///{tmp_path}/test.db",
        parquet_root=tmp_path / "data",
        plugins_dir=tmp_path / "plugins",
        strategies_file=tmp_path / "strategies.yaml",
        log_level="WARNING",  # 测试时安静
        log_json=False,
    )
    s.parquet_root.mkdir(parents=True, exist_ok=True)
    s.plugins_dir.mkdir(parents=True, exist_ok=True)
    return s


@pytest.fixture
def client(settings_for_test, monkeypatch) -> TestClient:
    """FastAPI TestClient + override settings + clear engine cache between tests."""
    from app.dependencies import _engine_for_url

    get_settings.cache_clear()
    _engine_for_url.cache_clear()
    app = create_app(settings_override=settings_for_test)
    return TestClient(app)
