"""live 专用 server API client。网络/协议异常一律抛出并停止。"""
from __future__ import annotations

import requests


class LiveServerClient:
    def __init__(
        self, base_url: str, api_key: str, timeout: int = 30,
        execution_domain: str = "live",
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.timeout = timeout
        self.execution_domain = execution_domain

    def fetch_orders(self, trade_date: str) -> list[dict]:
        response = requests.get(
            f"{self.base_url}/orders",
            params={"date": trade_date},
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"GET /orders 失败: {body}")
        return body.get("data", {}).get("orders") or []

    def stage_canary(self, payload: dict) -> dict:
        return self._post("/hydra/canary/stage", payload)

    def push_trade_results(self, trade_date: str, results: list[dict]) -> dict:
        response = requests.post(
            f"{self.base_url}/trade-result",
            json={
                "trade_date": trade_date,
                "execution_domain": self.execution_domain,
                "results": results,
            },
            headers={**self.headers, "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"POST /trade-result 失败: {body}")
        unmatched = body.get("data", {}).get("unmatched_order_ids") or []
        if unmatched:
            raise RuntimeError(f"成交回报存在 unmatched order_id: {unmatched}")
        return body["data"]

    def initialize_account(self, payload: dict) -> dict:
        return self._post("/accounts/initialize-from-qmt", payload)

    def strategy_ledger(self, instance_id: str, account_alias: str) -> dict:
        response = requests.get(
            f"{self.base_url}/accounts/strategy-ledger",
            params={"instance_id": instance_id, "account_alias": account_alias},
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"GET /accounts/strategy-ledger 失败: {body}")
        return body["data"]

    def reconcile(self, payload: dict) -> dict:
        return self._post("/admin/reconcile-positions", payload)

    def close_attempt(self, payload: dict) -> dict:
        return self._post("/hydra/attempts/close", payload)

    def stage_retry(self, payload: dict) -> dict:
        return self._post("/hydra/rebalances/retry", payload)

    def post_cash_flow(self, payload: dict) -> dict:
        return self._post("/cash-flows", payload)

    def _post(self, path: str, payload: dict) -> dict:
        response = requests.post(
            f"{self.base_url}{path}",
            json=payload,
            headers={**self.headers, "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"POST {path} 失败: {body}")
        return body["data"]
