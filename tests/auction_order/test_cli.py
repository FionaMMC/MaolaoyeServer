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


def _write_signal(db_path: Path, sid="s1", valid="20260422", **overrides):
    from src.common.db import get_connection, init_schema
    conn = get_connection(db_path)
    init_schema(conn)
    d = dict(
        signal_id=sid, symbol="600519.SH", direction="BUY", quantity=100,
        order_type="LIMIT", limit_price=1540.0, price_offset=0.005,
        strategy_id="s", signal_time="2026-04-21T18:30:00+08:00",
        valid_date=valid, fetched_at="2026-04-21T19:00:00+08:00",
    )
    d.update(overrides)
    conn.execute(
        """INSERT INTO signals
        (signal_id, symbol, direction, quantity, order_type, limit_price,
         price_offset, strategy_id, signal_time, valid_date, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (d["signal_id"], d["symbol"], d["direction"], d["quantity"],
         d["order_type"], d["limit_price"], d["price_offset"],
         d["strategy_id"], d["signal_time"], d["valid_date"], d["fetched_at"]),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def fake_xt(monkeypatch):
    trader = MagicMock()
    trader.start = MagicMock(return_value=None)
    trader.connect = MagicMock(return_value=0)
    trader.subscribe = MagicMock(return_value=0)
    trader.order_stock = MagicMock(return_value=111)

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


def test_cli_happy_path(fake_xt, tmp_path: Path, monkeypatch):
    from src.auction_order import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _write_signal(data_root / "trading.db", sid="s1", valid="20260422")
    _write_signal(data_root / "trading.db", sid="s2", valid="20260422")

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))

    exit_code = cli_mod.main(["--today", "20260422", "--config", str(cfg)])

    assert exit_code == 0
    assert fake_xt.trader.order_stock.call_count == 2
    assert any("下单完成" in c["text"]["content"] or "成功" in c["text"]["content"]
               for c in wecom_calls)


def test_cli_trader_connect_fail_alerts(fake_xt, tmp_path: Path, monkeypatch):
    from src.auction_order import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _write_signal(data_root / "trading.db", sid="s1", valid="20260422")

    fake_xt.trader.connect.return_value = -1

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))

    exit_code = cli_mod.main(["--today", "20260422", "--config", str(cfg)])

    assert exit_code == 2
    assert any(c["text"]["content"].startswith("[报警]") for c in wecom_calls)


def test_cli_market_signal_rejected_but_continues(fake_xt, tmp_path: Path, monkeypatch):
    from src.auction_order import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _write_signal(data_root / "trading.db", sid="good", valid="20260422")
    _write_signal(data_root / "trading.db", sid="bad", valid="20260422",
                  order_type="MARKET", limit_price=None)

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))

    exit_code = cli_mod.main(["--today", "20260422", "--config", str(cfg)])

    assert exit_code == 3
    assert fake_xt.trader.order_stock.call_count == 1
    assert any(c["text"]["content"].startswith("[报警]") for c in wecom_calls)


def test_cli_no_active_signals_ok(fake_xt, tmp_path: Path, monkeypatch):
    from src.auction_order import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _write_signal(data_root / "trading.db", sid="old", valid="20260421")

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(cli_mod, "_notify",
                        lambda webhook, msg, level: cli_mod.notify_wecom(
                            webhook, msg, level,
                            transport=httpx.MockTransport(wecom_handler)))

    exit_code = cli_mod.main(["--today", "20260422", "--config", str(cfg)])

    assert exit_code == 0
    fake_xt.trader.order_stock.assert_not_called()
