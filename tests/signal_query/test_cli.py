from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest


def _write_cfg(tmp: Path, data_root: str) -> Path:
    p = tmp / "settings.yaml"
    p.write_text(f"""
qmt:
  data_dir: "/tmp/fake_qmt"
  account_id: "ACC"
server:
  base_url: "https://srv"
  api_key: "KEY"
  timeout: 10
paths:
  data_root: "{data_root}"
  log_dir: "{data_root}/logs"
  sqlite_path: "{data_root}/trading.db"
notify:
  wecom_webhook: "https://wecom.example.com"
market_data:
  sector_name: "沪深A股"
""", encoding="utf-8")
    return p


@pytest.fixture
def fake_xtdata(monkeypatch):
    fake = SimpleNamespace(
        data_dir="",
        get_trading_dates=MagicMock(return_value=[
            "20260420", "20260421", "20260422", "20260423",
        ]),
    )
    monkeypatch.setitem(sys.modules, "xtquant", SimpleNamespace(xtdata=fake))
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake)
    return fake


def test_cli_has_signals(fake_xtdata, tmp_path: Path, monkeypatch):
    from src.signal_query import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))

    wecom_calls = []

    def server_handler(req):
        return httpx.Response(200, json={
            "code": 0, "data": {"date": "20260422", "signals": [{
                "signal_id": "s1", "symbol": "600519.SH",
                "direction": "BUY", "quantity": 100, "order_type": "LIMIT",
                "limit_price": 1540.0, "price_offset": 0.005,
                "strategy_id": "x", "signal_time": "2026-04-21T18:30:00+08:00",
                "valid_date": "20260422",
            }]}})

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_new_server_client",
                        lambda cfg: cli_mod.new_http_client(
                            cfg.server.base_url, cfg.server.api_key,
                            cfg.server.timeout,
                            transport=httpx.MockTransport(server_handler)))
    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))

    exit_code = cli_mod.main(["--today", "20260421", "--config", str(cfg)])

    assert exit_code == 0
    assert any("已制单" in c["text"]["content"] for c in wecom_calls)


def test_cli_no_signals(fake_xtdata, tmp_path: Path, monkeypatch):
    from src.signal_query import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))

    wecom_calls = []

    def server_handler(req):
        return httpx.Response(200, json={"code": 0,
                                         "data": {"signals": []}})

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_new_server_client",
                        lambda cfg: cli_mod.new_http_client(
                            cfg.server.base_url, cfg.server.api_key,
                            cfg.server.timeout,
                            transport=httpx.MockTransport(server_handler)))
    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))

    exit_code = cli_mod.main(["--today", "20260421", "--config", str(cfg)])

    assert exit_code == 0
    assert any("无交易信号" in c["text"]["content"] for c in wecom_calls)
    assert all(not c["text"]["content"].startswith("[报警]") for c in wecom_calls)


def test_cli_still_pending_alerts(fake_xtdata, tmp_path: Path, monkeypatch):
    from src.signal_query import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))

    wecom_calls = []

    def server_handler(req):
        return httpx.Response(200, json={"code": 3002, "message": "pending"})

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_new_server_client",
                        lambda cfg: cli_mod.new_http_client(
                            cfg.server.base_url, cfg.server.api_key,
                            cfg.server.timeout,
                            transport=httpx.MockTransport(server_handler)))
    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))
    monkeypatch.setattr("time.sleep", lambda s: None)

    exit_code = cli_mod.main([
        "--today", "20260421", "--config", str(cfg), "--wait-secs", "0",
    ])

    assert exit_code == 3
    assert any(c["text"]["content"].startswith("[报警]") for c in wecom_calls)


def test_cli_missing_config(tmp_path: Path):
    from src.signal_query import __main__ as cli_mod
    exit_code = cli_mod.main(["--today", "20260421",
                              "--config", str(tmp_path / "nope.yaml")])
    assert exit_code == 1
