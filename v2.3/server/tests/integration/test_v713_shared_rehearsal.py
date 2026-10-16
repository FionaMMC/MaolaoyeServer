"""Real FastAPI contracts + real account executor + a stock-aware synthetic broker."""
from dataclasses import replace
from datetime import datetime
import hashlib

from fastapi.testclient import TestClient

from app.dependencies import get_oms_cycle_service
from app.main import create_app
from app.models import InstanceState
from app.oms import api as oms_api
from app.oms.models import OmsCycle
from app.oms.planner import get_instrument
from app.oms.instruments import instrument_to_dict
from app.settings import Settings
from live_client.offline_acceptance import _config
from live_client.oms_agent import OmsAgent
from live_client.oms_journal import OmsJournal
from live_client.sim_exchange import SimExchange, SimQMTGateway
from tests.unit.test_oms_shared_account import ALIAS, HYDRA, V713, CAL, PRICES, setup_shared

LIVE = {"Authorization": "Bearer SYNTHETIC_LIVE"}
OPERATOR = {"Authorization": "Bearer SYNTHETIC_OPERATOR"}


class ApiAdapter:
    def __init__(self, client):
        self.client = client
        self.down = False

    def request(self, method, path, **kwargs):
        if self.down:
            raise ConnectionError("synthetic transport interruption")
        response = self.client.request(method, path, headers=LIVE, **kwargs)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["code"] == 0, payload
        return payload["data"]

    def get_oms_plans(self, alias, day, phase):
        return self.request("GET", "/oms/live/plans", params=dict(account_alias=alias, trade_date=day, phase=phase))

    def get_oms_universe(self, alias):
        return self.request("GET", "/oms/live/universe", params=dict(account_alias=alias))

    def post_oms_snapshot(self, payload):
        return self.request("POST", "/oms/live/snapshot", json=payload)

    def post_oms_events(self, alias, events):
        return self.request("POST", "/oms/live/events", json=dict(account_alias=alias, events=events))

    def get_oms_status(self, alias):
        return self.request("GET", "/oms/live/status", params=dict(account_alias=alias))

    def get_oms_manual_pending(self, alias, day):
        return self.request("GET", "/oms/live/manual/pending", params=dict(account_alias=alias, trade_date=day))

    def post_oms_manual_ack(self, alias, results):
        return self.request("POST", "/oms/live/manual/ack", json=dict(account_alias=alias, results=results))


def rehearsal(tmp_path, monkeypatch):
    sf, service, policy, hydra, v713 = setup_shared(tmp_path)
    cfg = replace(_config(tmp_path, "shared-rehearsal"), account_alias=ALIAS, execution_profile="shared_oms",
                  max_price_offset_bps=1000)
    service.account_fingerprint = cfg.expected_account_sha256
    specs = {symbol: instrument_to_dict(get_instrument(symbol, policy)) for symbol in PRICES}
    bars = {(day, symbol): dict(open=price, high=price*1.01, low=price*.99, close=price, volume=1_000_000)
            for day in CAL for symbol, price in PRICES.items()}
    exchange = SimExchange(bars, cash=30000., positions={"510300.SH": 1000, "000001.SZ": 1000},
                           instrument_specs=specs, slip_bps=0.)
    exchange.set_clock("20261019", "144500")
    monkeypatch.setattr(oms_api, "_now", lambda: exchange.now().isoformat())
    settings = Settings(_env_file=None, paper_api_key="SYNTHETIC_PAPER", live_api_key="SYNTHETIC_LIVE",
        oms_operator_api_key="SYNTHETIC_OPERATOR", live_client_id="synthetic-shared-client",
        live_account_aliases_csv=ALIAS, oms_live_enabled=True, live_qmt_account_sha256=cfg.expected_account_sha256,
        db_url=f"sqlite:///{tmp_path}/shared.db", scheduler_enabled=False,
        parquet_root=tmp_path/"data", plugins_dir=tmp_path/"plugins", strategies_file=tmp_path/"strategies.yaml")
    app = create_app(settings_override=settings)
    app.dependency_overrides[get_oms_cycle_service] = lambda: service
    client = TestClient(app)
    server = ApiAdapter(client)
    gateway = SimQMTGateway(exchange, account_id=cfg.account_id)
    gateway.connect()
    journal = OmsJournal(tmp_path/"oms-agent.db", clock=exchange.now)
    journal.bind(account_alias=ALIAS, execution_domain="live", account_fingerprint=cfg.expected_account_sha256,
                 client_id=cfg.task_prefix)
    agent = OmsAgent(account_alias=ALIAS, gateway=gateway, server=server, journal=journal, clock=exchange.now,
                     symbols=list(PRICES), cfg=cfg)
    return sf, service, agent, exchange, client, hydra, v713


def assert_account_matches(sf, exchange):
    with sf() as session:
        rows = session.query(InstanceState).filter_by(execution_domain="live", account_alias=ALIAS).all()
        positions = {}
        for row in rows:
            for symbol, quantity in row.virtual_positions.items():
                positions[symbol] = positions.get(symbol, 0) + quantity
        assert {s: q for s, q in positions.items() if q} == {s: row["volume"] for s, row in exchange.positions().items()}
        assert abs(sum(row.virtual_cash for row in rows) - exchange.cash) < 1e-6


def test_stock_rotation_shared_etf_and_full_window_match_broker(tmp_path, monkeypatch):
    sf, service, agent, exchange, client, hydra, v713 = rehearsal(tmp_path, monkeypatch)
    first = agent.pre("20261019")
    assert first["snapshot"]["reconciliation"]["passed"]
    exchange.set_clock("20261019", "145705")
    assert agent.execute("20261019", "SELL")["status"] == "EXECUTED"
    exchange.set_clock("20261019", "150500")
    assert agent.eod()["reconciliation"]["passed"]
    assert_account_matches(sf, exchange)
    with sf() as session:
        assert not session.get(InstanceState, V713).virtual_positions.get("000001.SZ")
    for day in CAL[2:-1]:
        exchange.set_clock(day, "090000")
        assert agent.pre(day)["snapshot"]["reconciliation"]["passed"]
        exchange.set_clock(day, "091505")
        agent.execute(day, "BUY")
        count = len(exchange.orders())
        agent.execute(day, "BUY")
        assert len(exchange.orders()) == count
        exchange.set_clock(day, "093000")
        agent.snapshot("ADHOC")
        assert_account_matches(sf, exchange)
        exchange.set_clock(day, "144500")
        agent.pre(day)
        exchange.set_clock(day, "145705")
        agent.execute(day, "SELL")
        exchange.set_clock(day, "150500")
        assert agent.eod()["reconciliation"]["passed"]
        assert_account_matches(sf, exchange)
    with sf() as session:
        a, b = session.get(InstanceState, HYDRA), session.get(InstanceState, V713)
        assert a.virtual_positions["510300.SH"] > 1000
        assert b.virtual_positions["510300.SH"] > 0
        assert b.virtual_positions["000002.SZ"] > 0
        assert a.virtual_cash >= 0 and b.virtual_cash >= 0
        assert session.get(OmsCycle, hydra["cycle_id"]).status == "CLOSED"
        assert session.get(OmsCycle, v713["cycle_id"]).status == "CLOSED"
    foreign = client.get("/oms/live/plans", params=dict(account_alias="foreign", trade_date="20261019", phase="SELL"),
                         headers=LIVE)
    assert foreign.status_code == 403


def test_lost_submit_response_restart_does_not_duplicate_stock_sale(tmp_path, monkeypatch):
    sf, service, agent, exchange, _, _, _ = rehearsal(tmp_path, monkeypatch)
    agent.pre("20261019")
    exchange.set_clock("20261019", "145705")
    exchange.faults.add("submit_raises_after_accept")
    agent.execute("20261019", "SELL")
    assert len(exchange.orders()) == 1
    agent.journal = OmsJournal(agent.journal.path, clock=exchange.now)
    exchange.faults.clear()
    agent.server.down = True
    agent.execute("20261019", "SELL")
    assert len(exchange.orders()) == 1
    agent.server.down = False
    exchange.set_clock("20261019", "150500")
    assert agent.eod()["reconciliation"]["passed"]
    assert_account_matches(sf, exchange)
