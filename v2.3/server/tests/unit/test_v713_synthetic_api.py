"""Read-only synthetic V713 endpoint and dashboard page."""
import json

from fastapi.testclient import TestClient

from app.dependencies import _engine_for_url, get_settings
from app.main import create_app
from app.settings import Settings

LIVE = {"Authorization": "Bearer LIVE_KEY"}
OPERATOR = {"Authorization": "Bearer OP_KEY"}


def _client(tmp_path, output):
    get_settings.cache_clear()
    _engine_for_url.cache_clear()
    settings = Settings(paper_api_key="PAPER_KEY", live_api_key="LIVE_KEY", oms_operator_api_key="OP_KEY",
                        live_client_id="hydra-live-client", live_account_aliases_csv="hydra-live",
                        oms_live_enabled=True, db_url=f"sqlite:///{tmp_path}/api.db",
                        parquet_root=tmp_path / "data", plugins_dir=tmp_path / "plugins",
                        strategies_file=tmp_path / "strategies.yaml", log_level="WARNING",
                        v713_synthetic_output=output)
    return TestClient(create_app(settings_override=settings))


def test_operator_reads_the_synthetic_document(tmp_path):
    output = tmp_path / "v713_synthetic.json"
    output.write_text(json.dumps({"policy_id": "V713_SYNTHETIC_V1", "lines": {}}), encoding="utf-8")
    client = _client(tmp_path, output)
    body = client.get("/oms/live/v713/synthetic", params={"account_alias": "hydra-live"}, headers=OPERATOR).json()
    assert body["code"] == 0 and body["data"]["policy_id"] == "V713_SYNTHETIC_V1"
    assert client.get("/oms/live/v713/synthetic", params={"account_alias": "hydra-live"},
                      headers=LIVE).status_code == 403


def test_missing_document_is_reported_not_invented(tmp_path):
    client = _client(tmp_path, tmp_path / "absent.json")
    response = client.get("/oms/live/v713/synthetic", params={"account_alias": "hydra-live"}, headers=OPERATOR)
    assert response.status_code == 404


def test_dashboard_page_is_served(tmp_path):
    page = _client(tmp_path, None).get("/dashboard/v713-synthetic")
    assert page.status_code == 200 and "V713 合成净值" in page.text
