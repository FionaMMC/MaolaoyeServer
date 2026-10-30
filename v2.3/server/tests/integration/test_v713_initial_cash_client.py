"""First V713 cash entry across real OMS APIs and the unchanged Windows executor.

All prices, cash, positions and broker events here are synthetic. No QMT or
external HTTP connection is constructed.
"""
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import init_db, make_engine, make_session_factory
from app.dependencies import get_oms_cycle_service
from app.main import create_app
from app.models import InstanceState
from app.oms import api as oms_api
from app.oms.cycles import CycleService
from app.oms.ledger import OrderLedger
from app.oms.models import OmsCycle, OmsOrder, OmsSession
from app.oms.v713_stock_leg import expand_policy
from app.services.reconcile import ReconcileService
from app.services.settlement import SettlementService
from app.settings import Settings
from live_client.offline_acceptance import _config
from live_client.oms_agent import OmsAgent
from live_client.oms_journal import JournalConflict, OmsJournal
from live_client.sim_exchange import SimExchange, SimQMTGateway
from tests.integration.test_v713_shared_rehearsal import ApiAdapter, assert_account_matches
from tests.unit.test_oms_shared_account import ALIAS, HYDRA


V713 = "live_v713_stockleg"
STOCK = "000001.SZ"
ETF = "510300.SH"
CALENDAR = ["20261030", "20261102", "20261103", "20261104", "20261105", "20261106",
            "20261109", "20261110", "20261111", "20261112", "20261113"]


@pytest.fixture
def initial_cash_rehearsal(tmp_path, monkeypatch):
    """Reuse the shared rehearsal's API transport, exchange and ownership check.

    Its ordinary rotation fixture already has stock holdings and cycle history,
    so first entry needs its own clean attributed strategy on the same account.
    """
    engine = make_engine(f"sqlite:///{tmp_path}/initial-cash.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as session:
        for instance, cash, positions in ((HYDRA, 10000., {ETF: 1000}), (V713, 20500., {})):
            session.add(InstanceState(instance_id=instance, execution_domain="live", account_alias=ALIAS,
                ledger_mode="attributed", virtual_cash=cash, virtual_positions=positions,
                owned_symbols=sorted(positions), strategy_state={"oms_enabled": True},
                last_update="2026-10-30T15:30:00+08:00"))
        session.commit()
    template = json.loads((Path(__file__).resolve().parents[2] /
                           "plugins/v713/stockleg_policy_template_v2.json").read_text())
    policy = expand_policy(template, [STOCK, ETF])
    cfg = replace(_config(tmp_path, "initial-cash"), account_alias=ALIAS,
                  execution_profile="shared_oms", mode="live")
    service = CycleService(sf, OrderLedger(sf, SettlementService(sf)), ReconcileService(sf),
                           account_fingerprint=cfg.expected_account_sha256)
    bars = {}
    for day in CALENDAR:
        for symbol, price in ((STOCK, 10.), (ETF, 4.)):
            close = 10.1 if symbol == STOCK and day >= "20261102" else price
            opening = close if day > "20261102" else price
            bars[day, symbol] = dict(open=opening, high=close * 1.01, low=price * .99,
                close=close, volume=400 if (day, symbol) == ("20261102", STOCK) else 1_000_000,
                up_limit=price * 1.1, down_limit=price * .9)
    exchange = SimExchange(bars, cash=30500., positions={ETF: 1000},
                           instrument_specs=policy["instruments"], slip_bps=0.)
    exchange.set_clock("20261030", "160000")
    monkeypatch.setattr(oms_api, "_now", lambda: exchange.now().isoformat())
    settings = Settings(_env_file=None, paper_api_key="SYNTHETIC_PAPER", live_api_key="SYNTHETIC_LIVE",
        oms_operator_api_key="SYNTHETIC_OPERATOR", live_client_id="synthetic-initial-cash",
        live_account_aliases_csv=ALIAS, oms_live_enabled=True,
        live_qmt_account_sha256=cfg.expected_account_sha256, db_url=f"sqlite:///{tmp_path}/initial-cash.db",
        scheduler_enabled=False, parquet_root=tmp_path/"data", plugins_dir=tmp_path/"plugins",
        strategies_file=tmp_path/"strategies.yaml")
    app = create_app(settings_override=settings)
    app.dependency_overrides[get_oms_cycle_service] = lambda: service
    client = TestClient(app)
    server = ApiAdapter(client)
    gateway = SimQMTGateway(exchange, account_id=cfg.account_id)
    gateway.connect()
    journal = OmsJournal(tmp_path/"oms-agent.db", clock=exchange.now)
    journal.bind(account_alias=ALIAS, execution_domain="live", account_fingerprint=cfg.expected_account_sha256,
                 client_id=cfg.task_prefix)

    def advance(seconds):
        next_time = exchange.now() + timedelta(seconds=seconds)
        exchange.set_clock(next_time.strftime("%Y%m%d"), next_time.strftime("%H%M%S"))

    agent = OmsAgent(account_alias=ALIAS, gateway=gateway, server=server, journal=journal,
                     clock=exchange.now, symbols=[STOCK, ETF], cfg=cfg, sleep=advance)
    evidence = {"trade_date": "20261030", "taken_at": "2026-10-30T15:30:00+08:00", "quotes": {
        STOCK: {"last_price": 10., "source_time": "2026-10-30T15:00:05+08:00",
                "adjustment": "none", "price_source": "raw_daily_close"}}}
    published = service.publish_target(instance_id=V713, account_alias=ALIAS, signal_date="20261030",
        weights={STOCK: 1.}, signal_closes={STOCK: 10.}, calendar=CALENDAR,
        source_sha256="a" * 64, now=exchange.now().isoformat(), policy=policy,
        earliest_execution_date="20261102", initial_cash_buy=True, reference_close_evidence=evidence)
    service.approve(published["cycle_id"], "synthetic-test", exchange.now().isoformat())
    yield SimpleNamespace(sf=sf, service=service, agent=agent, exchange=exchange,
                          published=published, server=server)
    gateway.close()
    client.close()
    engine.dispose()


def test_weekend_cached_first_buy_grant_cannot_execute_without_monday_refresh(initial_cash_rehearsal):
    run = initial_cash_rehearsal
    friday = run.agent.fetch_plans("20261102", "BUY")
    assert len(friday) == 1 and friday[0]["phase"] == "BUY"
    assert not friday[0]["executable"]
    assert friday[0]["lease_expires_at"].startswith("2026-10-30")
    run.exchange.set_clock("20261102", "091505")
    run.server.down = True
    # Restarting the Windows journal must not turn Friday's cache into authority.
    run.agent.journal = OmsJournal(run.agent.journal.path, clock=run.exchange.now)
    with pytest.raises(JournalConflict, match="expired execution grant: lease_expires_at"):
        run.agent.execute("20261102", "BUY")
    assert run.exchange.orders() == []
    assert run.agent.journal.submissions() == []
    assert_account_matches(run.sf, run.exchange)


def test_monday_pre_refresh_first_buy_and_partial_fill_eod_only_buy_remaining_target(initial_cash_rehearsal):
    run = initial_cash_rehearsal
    agent, exchange = run.agent, run.exchange
    initial = agent.fetch_plans("20261102", "BUY")[0]
    assert run.published["schedule"][0]["trade_date"] == "20261102"
    assert all(row["phase"] == "BUY" for row in run.published["schedule"])
    assert len(run.published["schedule"]) == 10
    assert initial["strategy_positions"] == {} and initial["strategy_cash"] == 20500.
    target = initial["frozen_target"][STOCK]
    assert target == 2000 and initial["orders"][0]["quantity"] == target

    exchange.set_clock("20261102", "090000")
    assert not agent.fetch_plans("20261102", "BUY")[0]["executable"]  # no same-day reconciliation yet
    pre = agent.pre("20261102")
    assert pre["snapshot"]["reconciliation"]["passed"] and pre["cached_plans"]["BUY"]
    refreshed = agent.journal.plans("20261102", "BUY")[0]
    assert refreshed["executable"] and refreshed["plan_sha256"] == initial["plan_sha256"]
    assert refreshed["lease_expires_at"] == "2026-11-02T09:16:00+08:00"

    # A same-day PRE grant remains usable during a short server interruption.
    exchange.set_clock("20261102", "091400")
    run.server.down = True
    result = agent.execute("20261102", "BUY")
    assert result["plans"][0]["orders"][0]["status"] == "SUBMITTED"
    assert exchange.now().strftime("%H:%M:%S") == "09:15:05"
    assert len(exchange.orders()) == 1
    agent.execute("20261102", "BUY")
    assert len(exchange.orders()) == 1  # journal intent prevents a second first order
    run.server.down = False
    agent.flush_events()

    exchange.set_clock("20261102", "150500")
    first_broker_order = exchange.orders()[0]
    assert first_broker_order["traded_volume"] == 400 < target
    first_eod = agent.eod()
    assert first_eod["reconciliation"]["passed"]
    assert "20261103:BUY" in first_eod["cached_upcoming"]
    assert_account_matches(run.sf, exchange)
    with run.sf() as session:
        own = session.get(InstanceState, V713)
        assert own.virtual_positions == {STOCK: 400}
        assert session.get(InstanceState, HYDRA).virtual_positions == {ETF: 1000}
        assert session.get(InstanceState, HYDRA).virtual_cash == 10000.
        first = session.get(OmsOrder, initial["orders"][0]["client_order_id"])
        assert first.state == "EXPIRED_DAY" and first.filled_qty == first.projected_qty == 400
        count = session.query(OmsSession).count()
        assert session.get(OmsCycle, run.published["cycle_id"]).frozen_target == {STOCK: target}
    exchange.set_clock("20261102", "153000")
    assert agent.eod()["reconciliation"]["passed"]
    with run.sf() as session:
        assert session.query(OmsSession).count() == count

    exchange.set_clock("20261103", "090000")
    assert agent.pre("20261103")["snapshot"]["reconciliation"]["passed"]
    residual = agent.journal.plans("20261103", "BUY")[0]
    assert residual["strategy_positions"] == {STOCK: 400}
    assert residual["frozen_target"] == {STOCK: target}
    # At the first EOD the broker still reports frozen cash for its status-55
    # remainder. The next plan respects that observed physical cash as well as
    # the strategy's own balance, so it cannot yet spend the full 1600-share gap.
    assert residual["orders"][0]["quantity"] == 1000 < target - 400
    assert residual["orders"][0]["limit_price"] == 10.15  # Monday's real synthetic close, not Friday's anchor
    exchange.set_clock("20261103", "091400")
    agent.execute("20261103", "BUY")
    assert len(exchange.orders()) == 1
    exchange.set_clock("20261103", "150500")
    assert agent.eod()["reconciliation"]["passed"]
    assert_account_matches(run.sf, exchange)
    with run.sf() as session:
        assert session.get(InstanceState, V713).virtual_positions == {STOCK: 1400}

    exchange.set_clock("20261104", "090000")
    assert agent.pre("20261104")["snapshot"]["reconciliation"]["passed"]
    remaining = agent.journal.plans("20261104", "BUY")[0]
    assert remaining["frozen_target"] == {STOCK: target}
    assert remaining["orders"][0]["quantity"] == target - 1400
    exchange.set_clock("20261104", "091400")
    agent.execute("20261104", "BUY")
    exchange.set_clock("20261104", "150500")
    assert agent.eod()["reconciliation"]["passed"]
    assert_account_matches(run.sf, exchange)
    with run.sf() as session:
        assert session.get(InstanceState, V713).virtual_positions == {STOCK: target}
        assert session.query(OmsOrder).filter_by(side="SELL").count() == 0
