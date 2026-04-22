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
  account_id: "ACC123"
server:
  base_url: "https://srv"
  api_key: "KEY"
  timeout: 10
paths:
  data_root: "{data_root}"
  log_dir: "{data_root}/logs"
  sqlite_path: "{data_root}/trading.db"
notify:
  wecom_webhook: "https://wecom"
market_data:
  sector_name: "沪深A股"
""", encoding="utf-8")
    return p


def _seed(db_path: Path):
    from src.common.db import get_connection, init_schema
    conn = get_connection(db_path)
    init_schema(conn)
    conn.execute(
        """INSERT INTO signals
        (signal_id, symbol, direction, quantity, order_type, limit_price,
         price_offset, strategy_id, signal_time, valid_date, fetched_at)
        VALUES ('s1', '600519.SH', 'BUY', 100, 'LIMIT', 10.0, 0.005,
                'strat', '2026-04-21T18:30:00+08:00', '20260422',
                '2026-04-21T19:00:00+08:00')""",
    )
    conn.execute(
        """INSERT INTO orders
        (order_id, signal_id, symbol, direction, submitted_price,
         submitted_quantity, submitted_at, submit_status)
        VALUES ('o1', 's1', '600519.SH', 'BUY', 10.05, 100,
                '2026-04-22T09:15:00+08:00', 'SUCCESS')""",
    )
    conn.commit()
    conn.close()


@pytest.fixture
def fake_xt(monkeypatch):
    trader = MagicMock()
    trader.start = MagicMock()
    trader.connect = MagicMock(return_value=0)
    trader.subscribe = MagicMock(return_value=0)
    trader.query_stock_trades = MagicMock(return_value=[
        SimpleNamespace(order_id=1, stock_code="600519.SH",
                        traded_price=10.0, traded_volume=100,
                        traded_amount=1000.0,
                        traded_time=1745284500),
    ])

    XtQuantTraderCls = MagicMock(return_value=trader)
    StockAccountCls = MagicMock(side_effect=lambda aid, t: SimpleNamespace(
        account_id=aid, account_type=t,
    ))

    fake_xttrader = SimpleNamespace(
        XtQuantTrader=XtQuantTraderCls,
        XtQuantTraderCallback=object,
    )
    fake_xttype = SimpleNamespace(StockAccount=StockAccountCls)
    fake_xtconstant = SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11)
    fake_xtdata = SimpleNamespace(data_dir="")

    pkg = SimpleNamespace(
        xttrader=fake_xttrader, xttype=fake_xttype,
        xtconstant=fake_xtconstant, xtdata=fake_xtdata,
    )
    for n, m in [
        ("xtquant", pkg),
        ("xtquant.xttrader", fake_xttrader),
        ("xtquant.xttype", fake_xttype),
        ("xtquant.xtconstant", fake_xtconstant),
        ("xtquant.xtdata", fake_xtdata),
    ]:
        monkeypatch.setitem(sys.modules, n, m)

    return SimpleNamespace(trader=trader)


def _patch_clients(monkeypatch, cli_mod, server_handler, wecom_handler):
    monkeypatch.setattr(cli_mod, "_new_server_client",
                        lambda cfg: cli_mod.new_http_client(
                            cfg.server.base_url, cfg.server.api_key,
                            cfg.server.timeout,
                            transport=httpx.MockTransport(server_handler)))
    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))


def test_cli_auction_happy_path(fake_xt, tmp_path: Path, monkeypatch):
    from src.trade_result import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _seed(data_root / "trading.db")

    server_calls = []

    def server_handler(req):
        import json
        server_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={
            "code": 0, "data": {"matched_count": 1,
                                "unmatched_signal_ids": []}})

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    _patch_clients(monkeypatch, cli_mod, server_handler, wecom_handler)

    exit_code = cli_mod.main([
        "--stage", "auction", "--today", "20260422",
        "--config", str(cfg),
    ])

    assert exit_code == 0
    assert len(server_calls) == 1
    assert server_calls[0]["stage"] == "auction"
    assert any("竞价成交" in c["text"]["content"] for c in wecom_calls)


def test_cli_close_stage(fake_xt, tmp_path: Path, monkeypatch):
    from src.trade_result import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _seed(data_root / "trading.db")

    server_calls = []

    def server_handler(req):
        import json
        server_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={
            "code": 0, "data": {"matched_count": 1,
                                "unmatched_signal_ids": []}})

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    _patch_clients(monkeypatch, cli_mod, server_handler, wecom_handler)

    exit_code = cli_mod.main([
        "--stage", "close", "--today", "20260422", "--config", str(cfg),
    ])

    assert exit_code == 0
    assert server_calls[0]["stage"] == "close"
    assert any("收盘成交" in c["text"]["content"] for c in wecom_calls)


def test_cli_trader_fail_alerts(fake_xt, tmp_path: Path, monkeypatch):
    from src.trade_result import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _seed(data_root / "trading.db")

    fake_xt.trader.connect.return_value = -1

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    _patch_clients(monkeypatch, cli_mod,
                   lambda req: httpx.Response(500), wecom_handler)

    exit_code = cli_mod.main([
        "--stage", "auction", "--today", "20260422", "--config", str(cfg),
    ])

    assert exit_code == 2
    assert any(c["text"]["content"].startswith("[报警]") for c in wecom_calls)


def test_cli_push_failure_alerts(fake_xt, tmp_path: Path, monkeypatch):
    from src.trade_result import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _seed(data_root / "trading.db")

    def server_handler(req):
        return httpx.Response(500)

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    _patch_clients(monkeypatch, cli_mod, server_handler, wecom_handler)
    monkeypatch.setattr("time.sleep", lambda s: None)

    exit_code = cli_mod.main([
        "--stage", "auction", "--today", "20260422", "--config", str(cfg),
    ])

    assert exit_code == 3
    assert any(c["text"]["content"].startswith("[报警]") for c in wecom_calls)


def test_cli_no_orders_nothing_to_report(fake_xt, tmp_path: Path, monkeypatch):
    from src.trade_result import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))

    server_called = []

    def server_handler(req):
        server_called.append(1)
        return httpx.Response(200, json={"code": 0, "data": {}})

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    _patch_clients(monkeypatch, cli_mod, server_handler, wecom_handler)

    exit_code = cli_mod.main([
        "--stage", "auction", "--today", "20260422", "--config", str(cfg),
    ])

    assert exit_code == 0
    assert server_called == []
