"""Run the isolated Hydra four-scenario smoke chain; never touches MiniQMT."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve()
V23 = HERE.parents[2]
SERVER = HERE.parents[1]
for path in (V23, SERVER):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pandas as pd
from fastapi.testclient import TestClient

from app.dependencies import _engine_for_url
from app.main import create_app
from app.models import Order
from app.schemas.hydra_data import HydraDataManifest
from app.schemas.hydra_relay import hydra_basket_hash
from app.services.hydra_data import HydraDataStore
from app.settings import Settings, get_settings
from live_client import cli
from live_client.config import LiveClientConfig
from live_client.gateway import classify_qmt_settlement_status

PUBLISHER_COMMIT = "8ebfd21a159c74b73397ffb3847878a597d055df"
PAPER_TOKEN = "HYDRA_SMOKE_PAPER_TOKEN"
ACCOUNT_ID = "HYDRA_SMOKE_ACCOUNT"
ACCOUNT_ALIAS = "hydra-smoke"
INSTANCE_ID = "hydra_smoke_v48"
SYMBOLS = [
    "510300.SH", "159915.SZ", "511260.SH", "518880.SH", "159981.SZ",
    "159985.SZ", "159930.SZ", "513500.SH", "513100.SH",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_snapshot(snapshot: Path) -> dict[str, dict]:
    streams = {
        "model_hfq": "hydra_model_hfq",
        "execution_raw": "hydra_execution_raw",
        "corporate_actions": "hydra_corporate_actions",
        "trading_calendar": "hydra_trading_calendar",
    }
    loaded: dict[str, dict] = {}
    for folder, stream in streams.items():
        data = snapshot / folder / "data.parquet"
        manifest_path = snapshot / folder / "manifest.json"
        if not data.is_file() or not manifest_path.is_file():
            raise FileNotFoundError(f"冻结包缺少 {folder}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["stream"] != stream or _sha256(data) != manifest["file_sha256"]:
            raise ValueError(f"冻结包 {folder} manifest/hash 不匹配")
        loaded[folder] = {"body": data.read_bytes(), "manifest": manifest}
    hfq = pd.read_parquet(snapshot / "model_hfq" / "data.parquet")
    raw = pd.read_parquet(snapshot / "execution_raw" / "data.parquet")
    if set(raw["symbol"]) != set(SYMBOLS) or "511010.SH" not in set(hfq["symbol"]):
        raise ValueError("Hydra 9 ETF / 511010 research-only 隔离不满足")
    return loaded


class _TestServerClient:
    """Routes the real live-client HTTP contract into an isolated FastAPI app."""

    test_client: TestClient

    def __init__(self, _base_url: str, _api_key: str, timeout: int = 30, execution_domain: str = "paper"):
        del timeout
        self.execution_domain = execution_domain

    def _request(self, method: str, path: str, **kwargs) -> dict:
        response = self.test_client.request(
            method, path,
            headers={"Authorization": f"Bearer {PAPER_TOKEN}"}, **kwargs,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"{method} {path} failed: {body}")
        return body.get("data") or {}

    def fetch_orders(self, trade_date: str) -> list[dict]:
        return self._request("GET", "/orders", params={"date": trade_date}).get("orders") or []

    def push_trade_results(self, trade_date: str, results: list[dict]) -> dict:
        return self._request("POST", "/trade-result", json={
            "trade_date": trade_date, "execution_domain": self.execution_domain,
            "results": results,
        })

    def initialize_account(self, payload: dict) -> dict:
        return self._request("POST", "/accounts/initialize-from-qmt", json=payload)

    def reconcile(self, payload: dict) -> dict:
        return self._request("POST", "/admin/reconcile-positions", json=payload)

    def close_attempt(self, payload: dict) -> dict:
        return self._request("POST", "/hydra/attempts/close", json=payload)

    def post_cash_flow(self, payload: dict) -> dict:
        return self._request("POST", "/cash-flows", json=payload)


def _config(root: Path) -> LiveClientConfig:
    account_hash = hashlib.sha256(ACCOUNT_ID.encode()).hexdigest()
    return LiveClientConfig(
        mode="mock_qmt", execution_domain="paper", account_id=ACCOUNT_ID,
        expected_account_sha256=account_hash, account_alias=ACCOUNT_ALIAS,
        instance_id=INSTANCE_ID, api_key=PAPER_TOKEN, server_base_url="http://hydra-smoke.invalid",
        userdata_dir=root / "never-used-userdata", session_id=910001,
        state_db=root / "client-state.db", log_dir=root / "logs", task_prefix="HydraSmoke",
        trading_enabled=True, allow_insecure_http=True, allowed_symbols=frozenset(SYMBOLS),
        max_daily_orders=0, max_single_order_notional=0, max_daily_buy_notional=0,
        max_daily_sell_notional=0, max_daily_turnover_notional=0, max_price_offset_bps=0,
        risk_mode="auto", auto_max_daily_orders=100, auto_buffer_bps=100,
    )


def _target(snapshot: dict[str, dict], as_of: str, execution_date: str) -> dict:
    payload = {
        "execution_domain": "paper", "account_alias": ACCOUNT_ALIAS, "instance_id": INSTANCE_ID,
        "strategy_version": "v48.0-TEST_ONLY", "publisher_source_commit": PUBLISHER_COMMIT,
        "decision_date": as_of, "as_of_date": as_of, "execution_date": execution_date,
        "research_input_hashes": {"test_only_weights": "1" * 64},
        "input_hashes": {
            "model_hfq": snapshot["model_hfq"]["manifest"]["file_sha256"],
            "execution_raw": snapshot["execution_raw"]["manifest"]["file_sha256"],
            "corporate_actions": snapshot["corporate_actions"]["manifest"]["file_sha256"],
            "trading_calendar": snapshot["trading_calendar"]["manifest"]["file_sha256"],
        },
        "weights": [{"code": code, "weight": 1 / len(SYMBOLS)} for code in SYMBOLS],
        "cash_buffer_weight": 0.0,
    }
    payload["basket_sha256"] = hydra_basket_hash(payload)
    return payload


def _run_scenario(name: str, snapshot: dict[str, dict], root: Path, as_of: str, execution_date: str, *, fill_ratios=None, reject_symbols=None) -> dict:
    scenario_root = root / name
    settings = Settings(
        paper_api_key=PAPER_TOKEN, paper_client_id="hydra-smoke-client",
        paper_account_aliases_csv=ACCOUNT_ALIAS,
        db_url=f"sqlite:///{scenario_root / 'server.db'}", parquet_root=scenario_root / "data",
        plugins_dir=scenario_root / "plugins", strategies_file=scenario_root / "strategies.yaml",
        hydra_allowed_publisher_commits_csv=PUBLISHER_COMMIT, scheduler_enabled=False,
        log_level="WARNING", log_json=False,
    )
    settings.parquet_root.mkdir(parents=True)
    settings.plugins_dir.mkdir()
    get_settings.cache_clear()
    _engine_for_url.cache_clear()
    app = create_app(settings_override=settings)
    store = HydraDataStore(settings.parquet_root)
    installed = {}
    for item in snapshot.values():
        manifest = HydraDataManifest.model_validate(item["manifest"])
        installed[manifest.stream] = store.install(item["body"], manifest).file_sha256
    mock_state = scenario_root / "mock-qmt.json"
    mock_state.write_text(json.dumps({
        "account_id": ACCOUNT_ID, "available_cash": 172_000.0, "total_asset": 200_000.0,
        "positions": {"510300.SH": 6000}, "sellable_positions": {"510300.SH": 6000},
        "fill_ratios": fill_ratios or {}, "reject_symbols": reject_symbols or [],
    }), encoding="utf-8")
    cfg = _config(scenario_root)
    previous = cli.LiveServerClient
    with TestClient(app) as test_client:
        _TestServerClient.test_client = test_client
        cli.LiveServerClient = _TestServerClient
        try:
            cli.initialize_account(cfg, "2" * 64, mock_state)
            staged = test_client.post(
                "/hydra/targets/stage", json=_target(snapshot, as_of, execution_date),
                headers={"Authorization": f"Bearer {PAPER_TOKEN}"},
            )
            staged.raise_for_status()
            stage_data = staged.json()["data"]
            queried = cli.query(cfg, execution_date)
            submitted = cli.submit(cfg, execution_date, mock_state)
            settled = cli.settle(cfg, execution_date, mock_state) if submitted["submitted"] else None
            orders = test_client.get(
                "/orders", params={"date": execution_date},
                headers={"Authorization": f"Bearer {PAPER_TOKEN}"},
            ).json()["data"]["orders"]
        finally:
            cli.LiveServerClient = previous
    return {
        "name": name, "installed_hashes": installed, "stage": stage_data,
        "query": queried, "submit": submitted, "settle": settled,
        "pending_orders_after": len(orders),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Hydra isolated mock integration smoke")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--execution-date", required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("smoke output 已存在，拒绝覆盖")
    snapshot = _load_snapshot(args.snapshot)
    args.output.mkdir(parents=True)
    reports = [
        _run_scenario("filled", snapshot, args.output, args.as_of, args.execution_date),
        _run_scenario("partial", snapshot, args.output, args.as_of, args.execution_date, fill_ratios={"159915.SZ": 0.5}),
        _run_scenario("rejected", snapshot, args.output, args.as_of, args.execution_date, reject_symbols=["159915.SZ"]),
    ]
    try:
        classify_qmt_settlement_status(SimpleNamespace(ORDER_REPORTED=1), 1, 0, 100)
    except RuntimeError:
        reports.append({"name": "active_or_unknown", "status": "PASS_FAIL_CLOSED"})
    else:
        raise AssertionError("活动/未知 QMT 订单未 fail closed")
    report = {
        "status": "PASS", "as_of_date": args.as_of, "execution_date": args.execution_date,
        "execution_domain": "paper", "qmt_mode": "mock_qmt", "scenarios": reports,
    }
    (args.output / "smoke-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
