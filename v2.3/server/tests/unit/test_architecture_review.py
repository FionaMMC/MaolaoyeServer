"""Architecture review page and shared meeting-note API."""
from __future__ import annotations

from app.review_catalog import REVIEW_CATALOG, review_item_ids


AUTH = {"Authorization": "Bearer TEST_KEY"}


def test_review_page_is_linked_and_contains_code_grounded_risk_material(client):
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert 'href="/dashboard/review"' in dashboard.text

    page = client.get("/dashboard/review")
    assert page.status_code == 200
    assert "Server 业务流程与风控闸门审阅" in page.text
    assert "常规 EOD 策略订单管线" in page.text
    assert "Hydra 专用 target / residual 执行链" in page.text
    assert "成交量不得超过委托量" in page.text
    assert "Canary 的账本归属" in page.text
    assert "接口面索引" in page.text
    assert "事实存储与写入边界" in page.text
    assert "/admin/architecture-review/session" in page.text


def test_every_risk_has_a_complete_discussion_contract_and_ids_are_unique():
    risks = REVIEW_CATALOG["risks"]
    assert len(risks) >= 70
    required = {
        "id", "phase", "name", "priority", "type", "threat", "control",
        "response", "likelihood", "now", "residual", "source",
    }
    for risk in risks:
        assert required == set(risk)
        assert all(str(risk[field]).strip() for field in required)

    reviewable = [
        item["id"]
        for group in ("flows", "boundaries", "risks", "questions")
        for item in REVIEW_CATALOG[group]
    ]
    assert len(reviewable) == len(set(reviewable)) == len(review_item_ids())


def test_review_note_api_requires_auth(client):
    response = client.get("/admin/architecture-review/session")
    assert response.status_code == 401


def test_review_comments_and_decisions_are_shared_and_persistent(client):
    initial = client.get(
        "/admin/architecture-review/session", headers=AUTH,
    ).json()["data"]
    assert initial["comments"] == []
    assert initial["decisions"] == []

    comment = client.post(
        "/admin/architecture-review/comments",
        headers=AUTH,
        json={
            "item_id": "set-07",
            "author": "Meican",
            "body": "Canary 成交必须先定义账本归属。",
        },
    )
    assert comment.status_code == 200
    assert comment.json()["data"]["item_id"] == "set-07"

    decision = client.put(
        "/admin/architecture-review/decisions/set-07",
        headers=AUTH,
        json={
            "status": "change_required",
            "rationale": "缺 mapping 时统一标记 bookkeeping_divergence。",
            "owner": "server",
            "updated_by": "同事",
        },
    )
    assert decision.status_code == 200
    assert decision.json()["data"]["status"] == "change_required"

    snapshot = client.get(
        "/admin/architecture-review/session", headers=AUTH,
    ).json()["data"]
    assert snapshot["updated_at"] is not None
    assert snapshot["comments"][0]["author"] == "Meican"
    assert snapshot["decisions"][0]["owner"] == "server"


def test_review_api_rejects_unknown_catalog_item(client):
    response = client.post(
        "/admin/architecture-review/comments",
        headers=AUTH,
        json={"item_id": "made-up-01", "author": "A", "body": "test"},
    )
    assert response.status_code == 404
    assert response.json()["code"] != 0
