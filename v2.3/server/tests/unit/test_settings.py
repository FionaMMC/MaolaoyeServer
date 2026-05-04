"""tests/unit/test_settings.py"""
import os
import pytest

from app.settings import Settings, get_settings


def _clear_qmt_env(monkeypatch):
    """清掉所有 QMT_* 环境变量，便于测试默认值。"""
    for k in list(os.environ.keys()):
        if k.startswith("QMT_"):
            monkeypatch.delenv(k)


def test_settings_default_values(monkeypatch):
    _clear_qmt_env(monkeypatch)
    s = Settings()
    assert s.api_key == ""
    assert s.host == "0.0.0.0"
    assert s.port == 8000
    assert s.db_url.startswith("sqlite")
    assert s.log_level == "INFO"
    assert s.log_json is False


def test_settings_from_env(monkeypatch):
    _clear_qmt_env(monkeypatch)
    monkeypatch.setenv("QMT_API_KEY", "PROD_KEY_xyz")
    monkeypatch.setenv("QMT_PORT", "9000")
    monkeypatch.setenv("QMT_LOG_JSON", "true")
    s = Settings()
    assert s.api_key == "PROD_KEY_xyz"
    assert s.port == 9000
    assert s.log_json is True


def test_get_settings_is_cached():
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
