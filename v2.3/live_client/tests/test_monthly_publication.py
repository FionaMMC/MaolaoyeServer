from live_client.monthly_publication import latest_completed_month_end
import json
from types import SimpleNamespace
import pandas as pd
import pytest
from live_client import monthly_publication as monthly


def test_no_new_cycle_on_mid_month_or_old_initial_baseline():
    calendar = [
        "20260831",
        "20260901",
        "20260918",
        "20260930",
        "20261008",
        "20261009",
        "20261030",
        "20261102",
    ]
    assert latest_completed_month_end(calendar, "20260918") is None
    assert latest_completed_month_end(calendar, "20260930") == "20260930"
    assert latest_completed_month_end(calendar, "20261008") == "20260930"
    assert latest_completed_month_end(calendar, "20261030") == "20261030"


@pytest.fixture
def publication(tmp_path, monkeypatch):
    cfg = SimpleNamespace(
        userdata_dir=tmp_path,
        state_db=tmp_path / "state.db",
        execution_domain="live",
        account_alias="test",
        instance_id="hydra",
    )
    xtdata = SimpleNamespace(
        get_trading_calendar=lambda *a, **kw: ["20260930", "20261008", "20261009"]
    )
    monkeypatch.setattr(monthly.ResearchDataConfig, "validate", lambda self: None)
    calls = []

    def prices(*args):
        calls.append("prices")
        return pd.DataFrame(), pd.DataFrame(), ["20260930", "20261008"]

    monkeypatch.setattr(monthly, "collect_prices", prices)
    monkeypatch.setattr(
        monthly, "collect_corporate_actions", lambda *a, **kw: pd.DataFrame()
    )
    monkeypatch.setattr(monthly, "producer_commit", lambda: "a" * 40)

    def bundle(frame, path, **kwargs):
        assert frame.attrs["hydra_as_of_date"] == "20260930"
        path.mkdir()
        (path / "data.parquet").write_bytes(b"frozen")
        (path / "manifest.json").write_text(json.dumps({"stream": kwargs["stream"]}))

    monkeypatch.setattr(monthly, "_write_bundle", bundle)
    return cfg, xtdata, calls


def test_lost_upload_response_reuses_frozen_bytes_then_cached_receipt(publication):
    cfg, xtdata, calls = publication
    payloads = []

    def post(route, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            raise TimeoutError("response lost")
        return {"status": "RESEARCH_SNAPSHOT_STORED", "as_of_date": "20260930"}

    server = SimpleNamespace(_post=post)
    with pytest.raises(TimeoutError):
        monthly.publish_monthly_data(cfg, "20260930", server, xtdata)
    result = monthly.publish_monthly_data(cfg, "20261008", server, xtdata)
    assert monthly.publish_monthly_data(cfg, "20261009", server, xtdata) == result
    assert payloads[0] == payloads[1] and calls == ["prices"]


def test_missing_month_end_archive_does_not_reconstruct_old_hfq(publication):
    cfg, xtdata, calls = publication
    with pytest.raises(RuntimeError, match="冒充"):
        monthly.publish_monthly_data(cfg, "20261008", None, xtdata)
    assert calls == []


def test_unconfirmed_upload_does_not_save_receipt(publication):
    cfg, xtdata, calls = publication
    with pytest.raises(RuntimeError, match="回执"):
        monthly.publish_monthly_data(
            cfg, "20260930", SimpleNamespace(_post=lambda *a: {}), xtdata
        )
    assert not list(cfg.state_db.parent.rglob("upload-receipt.json"))
