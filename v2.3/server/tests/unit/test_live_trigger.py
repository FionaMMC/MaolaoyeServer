"""实盘 trigger 只能生成 live 域订单，且 token 不具备其他权限。"""
from app.dependencies import get_strategy_pipeline
from app.main import create_app
from app.settings import Settings
from fastapi.testclient import TestClient


def _client() -> TestClient:
    settings = Settings(
        paper_api_key="PAPER_KEY",
        live_api_key="LIVE_KEY",
        live_trigger_api_key="TRIGGER_KEY",
        live_client_id="hydra-live-client",
        live_account_aliases_csv="hydra-live",
        log_level="WARNING",
    )
    return TestClient(create_app(settings_override=settings))


def test_live_trigger_runs_pipeline_in_live_domain():
    client = _client()
    calls = {}

    class SpyPipeline:
        def run(self, trade_date, force=False, execution_domain="paper"):
            calls.update({
                "trade_date": trade_date,
                "force": force,
                "execution_domain": execution_domain,
            })
            return {"trade_date": trade_date, "orders": 0, "execution_domain": execution_domain}

    client.app.dependency_overrides[get_strategy_pipeline] = lambda: SpyPipeline()
    try:
        response = client.post(
            "/hydra/live/trigger?trade_date=20260901",
            headers={"Authorization": "Bearer TRIGGER_KEY"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["code"] == 0
        assert calls == {
            "trade_date": 20260901,
            "force": False,
            "execution_domain": "live",
        }
    finally:
        client.app.dependency_overrides.pop(get_strategy_pipeline, None)


def test_live_trigger_token_cannot_use_admin_pipeline_route():
    response = _client().post(
        "/admin/run-pipeline?trade_date=20260901",
        headers={"Authorization": "Bearer TRIGGER_KEY"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == 1001
