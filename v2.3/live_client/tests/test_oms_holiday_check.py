from live_client.oms_holiday_check import PROBE_REMARK, readonly_report, reject_probe
from live_client.sim_exchange import SimExchange, SimQMTGateway

BARS = {("20261009", "510300.SH"): dict(open=4.6, high=4.62, low=4.58, close=4.61, volume=1_000_000)}


def _gateway(day, hhmmss):
    exchange = SimExchange(BARS, cash=100000.0, positions={"510300.SH": 1000})
    exchange.set_clock(day, hhmmss)
    gateway = SimQMTGateway(exchange)
    gateway.connect()
    return exchange, gateway


def test_readonly_report_never_submits():
    exchange, gateway = _gateway("20261007", "100000")          # a holiday: no bars that day
    report = readonly_report(gateway, ["510300.SH"])
    assert report["positions"] == {"510300.SH": 1000} and report["orders_today"] == 0
    assert exchange.orders() == []


def test_reject_probe_on_a_holiday_is_refused_and_records_remark_handling():
    exchange, gateway = _gateway("20261007", "100000")
    out = reject_probe(gateway, sleep=lambda _: None)
    assert out["submit_status"] == "REJECTED"
    assert out["sent_remark_length"] == len(PROBE_REMARK)
    assert out["cancelled"] == [] and exchange.positions()["510300.SH"]["volume"] == 1000
