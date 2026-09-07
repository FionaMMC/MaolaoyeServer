"""The new explanatory page must never become a trading control surface."""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.api.architecture_blueprint import (
    ASSET_ROOT,
    SESSION_ID,
    blueprint_catalog,
    blueprint_item_ids,
)

AUTH = {"Authorization": "Bearer TEST_KEY"}


def test_page_is_accessible_and_linked_from_both_existing_pages(client):
    for path in ("/dashboard", "/dashboard/review"):
        assert 'href="/dashboard/blueprint"' in client.get(path).text
    response = client.get("/dashboard/blueprint")
    assert response.status_code == 200
    assert "不是实时账户" in response.text
    assert "每一笔钱" in response.text
    assert "__BLUEPRINT_CATALOG__" not in response.text
    assert "http://120.26" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"


def test_static_assets_and_template_are_packaged_and_allowlisted(client):
    for name in ("blueprint.css", "model.js", "blueprint.js"):
        response = client.get(f"/dashboard/blueprint/assets/{name}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
    assert client.get("/dashboard/blueprint/assets/catalog.json").status_code == 404
    assert client.get("/dashboard/blueprint/assets/auth.py").status_code == 404
    assert client.get("/dashboard/blueprint/assets/%2e%2e%2fauth.py").status_code == 404
    html = (ASSET_ROOT / "index.html").read_text()
    assert "https://" not in html  # No CDN, external font or diagram dependency.
    assert "prefers-reduced-motion" in (ASSET_ROOT / "blueprint.css").read_text()


def test_all_26_gates_are_complete_and_connected_to_the_spec():
    c = blueprint_catalog()
    assert [g["code"] for g in c["gates"]] == [f"G{i:02}" for i in range(1, 27)]
    assert len(c["stages"]) == 10
    assert len(c["scenarios"]) == 15
    assert len(c["revisions"]) == 6
    assert len(blueprint_item_ids()) == 42
    assert len({x["id"] for x in c["scenarios"]}) == len(c["scenarios"])
    for g in c["gates"]:
        for field in ("id", "code", "name", "phase", "kind", "modes", "threat", "rule", "scope", "continues", "recovery", "current", "evidence", "section"):
            assert g[field], (g["id"], field)
        assert set(g["modes"]) <= {"shadow", "paper", "live"}
    for group in ("stages", "scenarios"):
        for item in c[group]:
            assert set(item["gates"]) <= blueprint_item_ids()
    spec = Path(__file__).resolve().parents[4] / "docs" / c["meta"]["spec"]
    assert spec.is_file()
    for g in c["gates"]:
        assert f"| {g['code']} |" in spec.read_text()


def test_page_does_not_open_note_store_or_trading_services(client):
    from app.api.architecture_blueprint import get_blueprint_review_service

    def must_not_run():
        raise AssertionError("Reading illustrative material must not open a DB service")

    client.app.dependency_overrides[get_blueprint_review_service] = must_not_run
    assert client.get("/dashboard/blueprint").status_code == 200
    script = (ASSET_ROOT / "blueprint.js").read_text()
    assert re.search(r'''fetch\(\s*["']/admin/architecture-blueprint["']''', script)
    for path in ("/orders", "/cash-flows", "/trade-result", "/hydra/targets", "/hydra/rebalances", "/hydra/attempts"):
        assert path not in script
    assert "localStorage.setItem('qmt_api_key'" not in script


def test_shared_notes_require_existing_admin_auth(client):
    assert client.get("/admin/architecture-blueprint/session").status_code == 401
    assert client.put("/admin/architecture-blueprint/decisions/g09", json={"updated_by": "A"}).status_code == 401


def test_new_shared_notes_do_not_mix_with_old_review_or_trade_tables(client, settings_for_test):
    first = client.get("/admin/architecture-blueprint/session", headers=AUTH).json()["data"]
    assert first["session_id"] == SESSION_ID
    assert first["decisions"] == []
    payload = {"status": "change_required", "rationale": "等待成交不能挡住撤单。", "owner": "Windows", "updated_by": "规则讨论"}
    decision = client.put("/admin/architecture-blueprint/decisions/revision-01", headers=AUTH, json=payload)
    assert decision.status_code == 200
    assert decision.json()["data"]["rationale"] == payload["rationale"]
    assert client.post("/admin/architecture-blueprint/comments", headers=AUTH, json={"item_id": "g09", "author": "A", "body": "预留不扣净值"}).status_code == 200
    again = client.get("/admin/architecture-blueprint/session", headers=AUTH).json()["data"]
    assert len(again["decisions"]) == len(again["comments"]) == 1
    old = client.get("/admin/architecture-review/session", headers=AUTH).json()["data"]
    assert old["decisions"] == old["comments"] == []
    from app.dependencies import _engine_for_url
    from sqlalchemy import text

    engine = _engine_for_url(settings_for_test.db_url)
    with engine.connect() as conn:
        for table in ("orders", "trades", "cash_flow_journal", "instance_state"):
            assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0


def test_live_execution_key_cannot_write_review_notes(client, settings_for_test):
    settings_for_test.live_api_key = "EXECUTION_ONLY_TEST_KEY"
    settings_for_test.live_client_id = "scope-test"
    settings_for_test.live_account_aliases_csv = "scope-test-account"
    response = client.get("/admin/architecture-blueprint/session", headers={"Authorization": "Bearer EXECUTION_ONLY_TEST_KEY"})
    assert response.status_code == 403


def test_new_review_rejects_unknown_old_catalog_ids_and_blank_notes(client):
    response = client.post("/admin/architecture-blueprint/comments", headers=AUTH, json={"item_id": "set-07", "author": "A", "body": "属于旧审阅"})
    assert response.status_code == 404
    bad = client.put("/admin/architecture-blueprint/decisions/g09", headers=AUTH, json={"updated_by": "   "})
    assert bad.json()["code"] != 0


def test_catalog_is_safe_to_embed_as_json_script():
    dumped = json.dumps(blueprint_catalog(), ensure_ascii=False).replace("<", "\\u003c")
    assert "</script" not in dumped.lower()
    assert json.loads(dumped)["meta"]["version"] == "1.1"
