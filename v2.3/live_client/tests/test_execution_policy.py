"""Offline queue and raw execution publication; no real QMT/server access."""

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from live_client import cli, execution_publication
from live_client.core import validate_order_batch
from live_client.queue_runner import CHINA, run_queue_passes
from live_client.tests.test_live_client import _cfg, _orders, _freeze_with_preflight


def test_cash_queue_resumes_only_waiting_unsent_orders():
    clock = [datetime(2026, 9, 10, 9, 10, tzinfo=CHINA)]
    calls = []

    def one_pass():
        calls.append(clock[0])
        return (
            {"status": "WAITING_FOR_CASH"} if len(calls) < 3 else {"submitted_now": 1}
        )

    def wait(seconds):
        clock[0] += timedelta(seconds=seconds)

    result = run_queue_passes(one_pass, "20260910", now=lambda: clock[0], wait=wait)
    assert result["status"] == "SUBMIT_QUEUE_COMPLETE"
    assert result["passes"] == 3
    assert calls[-1] - calls[0] == timedelta(seconds=60)


def test_automatic_queue_uses_real_client_state_without_server(tmp_path, monkeypatch):
    from test_execution_queue import _setup

    cfg, _, state, gateway = _setup(tmp_path, monkeypatch)

    def cash_arrives(seconds):
        assert seconds == 30
        assert gateway.calls == ["SELL"]
        assert state.submission("ho_0")["submit_status"] == "DEFERRED_CASH"
        gateway.filled = 100

    result = run_queue_passes(
        lambda: cli.submit(cfg, "20260803", None),
        "20260803",
        now=lambda: datetime(2026, 8, 3, 9, 10, tzinfo=CHINA),
        wait=cash_arrives,
    )
    assert result["passes"] == 2
    assert gateway.calls == ["SELL", "BUY"]


def test_queue_never_retries_ambiguous_submission():
    calls = []

    def one_pass():
        calls.append(1)
        raise RuntimeError("broker reply lost")

    with pytest.raises(RuntimeError, match="reply lost"):
        run_queue_passes(
            one_pass, "20260910", now=lambda: datetime(2026, 9, 10, 9, 10, tzinfo=CHINA)
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "hour,minute,status",
    [
        (9, 9, "WAITING_EXECUTION_WINDOW"),
        (14, 55, "QUEUE_WINDOW_ENDED"),
        (15, 0, "QUEUE_WINDOW_ENDED"),
    ],
)
def test_queue_window_never_calls_submit(hour, minute, status):
    def forbidden():
        pytest.fail("out-of-window broker access")

    result = run_queue_passes(
        forbidden,
        "20260910",
        now=lambda: datetime(2026, 9, 10, hour, minute, tzinfo=CHINA),
    )
    assert result["status"] == status


def test_cash_wait_stops_exactly_before_cancellation():
    clock = [datetime(2026, 9, 10, 14, 54, 50, tzinfo=CHINA)]
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    result = run_queue_passes(
        lambda: {"status": "WAITING_FOR_CASH"},
        "20260910",
        now=lambda: clock[0],
        wait=wait,
    )
    assert waits == [10]
    assert result["status"] == "QUEUE_WINDOW_ENDED" and result["passes"] == 1


def test_inner_submit_cutoff_after_slow_snapshot(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _freeze_with_preflight(cfg)
    clock = [datetime(2026, 8, 3, 14, 54, tzinfo=CHINA)]

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(cli, "datetime", FakeDateTime)
    from live_client.gateway import AccountSnapshot

    class Gateway:
        def connect(self):
            pass

        def close(self):
            pass

        def find_existing_submission(self, order):
            return None

        def account_snapshot(self):
            clock[0] = clock[0].replace(hour=14, minute=55)
            return AccountSnapshot(
                account_id=cfg.account_id,
                available_cash=10000,
                total_asset=10000,
                positions={},
                sellable_positions={},
            )

        def submit(self, order):
            pytest.fail("new broker call after 14:55")

    monkeypatch.setattr(cli, "_gateway", lambda *_: Gateway())
    result = cli.submit(
        cfg, "20260803", None, stop_new_at=clock[0].replace(hour=14, minute=55)
    )
    assert result["status"] == "QUEUE_WINDOW_ENDED"


class Market:
    def __init__(self, dates, missing=False):
        self.dates = dates
        self.missing = missing
        self.calls = []

    def get_trading_calendar(self, *args, **kwargs):
        return self.dates

    def download_history_data2(self, *args):
        self.calls.append(("download", args))

    def get_market_data_ex(self, fields, symbols, period, start, end, **kwargs):
        self.calls.append(("read", kwargs))
        return {
            symbol: pd.DataFrame(
                [
                    dict(
                        open=2.0,
                        high=2.0,
                        low=2.0,
                        close=2.0,
                        volume=100,
                        amount=200,
                        suspendFlag=0,
                    )
                ],
                index=[start],
            )
            for symbol in (symbols[:-1] if self.missing else symbols)
        }


def test_publication_reads_only_unadjusted_daily_bars(tmp_path, monkeypatch):
    monkeypatch.setattr(execution_publication, "producer_commit", lambda: "a" * 40)
    market = Market(["20260910", "20260911"])
    cfg = _cfg(tmp_path)
    result = execution_publication.collect_execution_publication(
        cfg, "20260910", market
    )
    assert len(result["bars"]) == len(cfg.allowed_symbols)
    assert {bar["trade_date"] for bar in result["bars"]} == {"20260910"}
    assert market.calls[-1] == ("read", {"dividend_type": "none", "fill_data": False})
    assert result["account_alias"] == cfg.account_alias


def test_publication_holiday_and_missing_bars(tmp_path):
    cfg = _cfg(tmp_path)
    market = Market(["20260914", "20260915"])
    assert (
        execution_publication.collect_execution_publication(cfg, "20260912", market)
        is None
    )
    assert market.calls == []
    with pytest.raises(RuntimeError, match="缺少"):
        execution_publication.collect_execution_publication(
            cfg, "20260910", Market(["20260910", "20260911"], missing=True)
        )


def policy_orders():
    orders = _orders("20260911")
    policy = dict(
        policy_id="HYDRA_ADJACENT_DAY_50BP_V1",
        reference_date="20260910",
        buy_max_bps=50,
        sell_max_bps=50,
        execution_raw_sha256="a" * 64,
        execution_calendar_sha256="b" * 64,
    )
    canonical = [
        dict(
            symbol=o["symbol"],
            direction=o["direction"],
            quantity=o["quantity"],
            reference_price=o["execution_reference_price"],
            limit_price=o["limit_price"],
        )
        for o in orders
    ]
    sha = hashlib.sha256(
        json.dumps(
            dict(
                rebalance_id="hr_test",
                attempt_number=1,
                trade_date="20260911",
                orders=sorted(canonical, key=lambda o: o["symbol"]),
                execution_policy=policy,
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    for order in orders:
        order.update(
            execution_policy=dict(policy), batch_sha256=sha, batch_id="hb_" + sha
        )
    return orders


def test_policy_is_bound_to_frozen_batch(tmp_path):
    cfg = _cfg(tmp_path)
    assert validate_order_batch(policy_orders(), "20260911", cfg)
    for field, value in [
        ("reference_date", "20260909"),
        ("buy_max_bps", 100),
        ("execution_raw_sha256", "c" * 64),
    ]:
        orders = policy_orders()
        for order in orders:
            order["execution_policy"][field] = value
        with pytest.raises(ValueError):
            validate_order_batch(orders, "20260911", cfg)


def test_windows_automatic_queue_and_evening_wait_contracts():
    root = Path(cli.__file__).parent / "windows"
    submit = (root / "Invoke-HydraLiveSubmit.ps1").read_text()
    operations = (root / "Invoke-HydraLiveOperations.ps1").read_text()
    tasks = (root / "Register-HydraLiveOperationsTasks.ps1").read_text()
    registrar = (root / "Register-HydraLiveSubmitTask.ps1").read_text()
    assert "-Command submit-queue" in submit
    assert "-Command preflight" not in submit and "-Command advance" not in submit
    assert "-Minutes 360" in registrar
    assert 'Stage = "publish-execution"' in tasks
    assert operations.index(
        "if ($operationPending -or $operationWaitingDate)"
    ) < operations.index("$nextDate = Get-NextTradingDate")
