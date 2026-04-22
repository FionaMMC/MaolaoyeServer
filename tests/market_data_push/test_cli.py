from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pandas as pd
import pytest


def _write_cfg(tmp: Path, data_root: str) -> Path:
    p = tmp / "settings.yaml"
    p.write_text(f"""
qmt:
  data_dir: "/tmp/fake"
  account_id: "ACC"
server:
  base_url: "https://srv.example.com"
  api_key: "KEY"
  timeout: 10
paths:
  data_root: "{data_root}"
  log_dir: "{data_root}/logs"
  sqlite_path: "{data_root}/trading.db"
notify:
  wecom_webhook: "https://wecom.example.com/hook"
market_data:
  sector_name: "沪深A股"
""", encoding="utf-8")
    return p


def _write_parquet(data_root: Path, date: str, n: int = 2) -> Path:
    mdir = data_root / "market_data"
    mdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(n):
        rows.append(dict(
            symbol=f"{600000 + i}.SH", trade_date=date,
            open=10.0, high=10.5, low=9.8, close=10.2,
            volume=1000, amount=10000.0,
            turnover_rate=0.01, is_suspended=False,
        ))
    df = pd.DataFrame(rows)
    df["volume"] = df["volume"].astype("int64")
    df["is_suspended"] = df["is_suspended"].astype("bool")
    out = mdir / f"{date}.parquet"
    df.to_parquet(out, index=False)
    return out


def test_cli_happy_path(tmp_path: Path, monkeypatch):
    from src.market_data_push import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _write_parquet(data_root, "20260422", n=3)

    fake_server_calls = []

    def server_handler(req: httpx.Request) -> httpx.Response:
        fake_server_calls.append(str(req.url))
        return httpx.Response(200, json={
            "code": 0, "data": {"trade_date": "20260422",
                                "received_count": 3,
                                "strategy_triggered": True}})

    wecom_calls = []

    def wecom_handler(req: httpx.Request) -> httpx.Response:
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(
        cli_mod, "_new_server_client",
        lambda cfg: cli_mod.new_http_client(
            cfg.server.base_url, cfg.server.api_key, cfg.server.timeout,
            transport=httpx.MockTransport(server_handler)),
    )
    monkeypatch.setattr(
        cli_mod, "_notify",
        lambda webhook, msg, level: cli_mod.notify_wecom(
            webhook, msg, level, transport=httpx.MockTransport(wecom_handler)),
    )

    exit_code = cli_mod.main(["--date", "20260422", "--config", str(cfg)])

    assert exit_code == 0
    assert len(fake_server_calls) == 1
    assert any("/market-data" in u for u in fake_server_calls)
    assert len(wecom_calls) == 1
    assert "成功" in wecom_calls[0]["text"]["content"]


def test_cli_missing_parquet(tmp_path: Path, monkeypatch):
    from src.market_data_push import __main__ as cli_mod

    cfg = _write_cfg(tmp_path, str(tmp_path / "data"))

    exit_code = cli_mod.main(["--date", "20260422", "--config", str(cfg)])
    assert exit_code == 2


def test_cli_push_failed_triggers_alert(tmp_path: Path, monkeypatch):
    from src.market_data_push import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _write_parquet(data_root, "20260422", n=2)

    def server_handler(req):
        return httpx.Response(500, text="boom")

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(
        cli_mod, "_new_server_client",
        lambda cfg: cli_mod.new_http_client(
            cfg.server.base_url, cfg.server.api_key, cfg.server.timeout,
            transport=httpx.MockTransport(server_handler)),
    )
    monkeypatch.setattr(
        cli_mod, "_notify",
        lambda webhook, msg, level: cli_mod.notify_wecom(
            webhook, msg, level, transport=httpx.MockTransport(wecom_handler)),
    )
    monkeypatch.setattr("time.sleep", lambda s: None)

    exit_code = cli_mod.main(["--date", "20260422", "--config", str(cfg)])

    assert exit_code == 3
    assert len(wecom_calls) == 1
    assert wecom_calls[0]["text"]["content"].startswith("[报警]")
