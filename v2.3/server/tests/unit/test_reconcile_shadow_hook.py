"""Plan 2 Task 4: /admin/reconcile-positions 调 shadow_compare（log-only），
且 shadow 的任何异常都不得影响权威 reconcile 结果。"""
import asyncio
from unittest.mock import MagicMock

from app.api.admin_query import reconcile_positions
from app.schemas.reconcile import QmtPositionSnapshot, ReconcileResult


def _snap() -> QmtPositionSnapshot:
    return QmtPositionSnapshot(
        instance_id="paper_v53_v53",
        qmt_account_id="A",
        qmt_cash=1.0,
        qmt_positions={"511260.SH": 1},
        snapshot_time="t",
        dry_run=True,
    )


def _result() -> ReconcileResult:
    return ReconcileResult(
        instance_id="paper_v53_v53", snapshot_time="t", dry_run=True, applied=False,
        server_cash=1.0, qmt_cash=1.0, cash_diff=0.0,
        n_server_positions=0, n_qmt_positions=1, n_matched=0, n_mismatched=0,
        n_server_only=0, n_qmt_only=1, diffs=[],
    )


def test_endpoint_invokes_shadow_compare():
    svc = MagicMock()
    svc.reconcile.return_value = _result()
    resp = asyncio.run(reconcile_positions(_snap(), service=svc))
    svc.shadow_compare.assert_called_once()
    assert resp.code == 0
    assert resp.data.instance_id == "paper_v53_v53"


def test_shadow_failure_does_not_break_reconcile():
    svc = MagicMock()
    svc.reconcile.return_value = _result()
    svc.shadow_compare.side_effect = RuntimeError("boom")
    # 必须不抛：shadow 失败被吞，权威结果照常返回
    resp = asyncio.run(reconcile_positions(_snap(), service=svc))
    assert resp.code == 0
    assert resp.data.instance_id == "paper_v53_v53"
