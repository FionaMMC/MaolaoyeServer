"""ReconcileService 测试"""
from datetime import datetime
from pathlib import Path

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState
from app.schemas.reconcile import QmtPositionSnapshot
from app.services.reconcile import InstanceNotFound, ReconcileService


def _factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    return make_session_factory(engine)


def _seed_instance(sf, instance_id: str, cash: float, positions: dict[str, int]):
    with sf() as s:
        s.add(InstanceState(
            instance_id=instance_id,
            virtual_cash=cash,
            virtual_positions=positions,
            last_update=datetime.now().isoformat(),
        ))
        s.commit()


def _snapshot(instance_id: str, qmt_cash: float, qmt_positions: dict[str, int],
              dry_run: bool = True) -> QmtPositionSnapshot:
    return QmtPositionSnapshot(
        instance_id=instance_id,
        qmt_account_id="TEST123",
        qmt_cash=qmt_cash,
        qmt_positions=qmt_positions,
        snapshot_time=datetime.now().isoformat(),
        dry_run=dry_run,
    )


def test_reconcile_instance_not_found(tmp_path: Path):
    sf = _factory(tmp_path)
    svc = ReconcileService(sf)
    with pytest.raises(InstanceNotFound):
        svc.reconcile(_snapshot("nonexistent", 1000.0, {}))


def test_reconcile_dryrun_perfect_match(tmp_path: Path):
    """server 和 QMT 完全一致 → diff 列表为空，applied=False，state 不变。"""
    sf = _factory(tmp_path)
    _seed_instance(sf, "inst", 1_000_000.0, {"600519.SH": 100, "000001.SZ": 200})
    svc = ReconcileService(sf)

    snap = _snapshot("inst", 1_000_000.0, {"600519.SH": 100, "000001.SZ": 200})
    result = svc.reconcile(snap)

    assert result.dry_run is True
    assert result.applied is False
    assert result.cash_diff == 0.0
    assert result.n_matched == 2
    assert result.n_mismatched == 0
    assert result.n_server_only == 0
    assert result.n_qmt_only == 0
    assert result.diffs == []

    # state 没变
    with sf() as s:
        inst = s.get(InstanceState, "inst")
        assert inst.virtual_cash == 1_000_000.0


def test_reconcile_dryrun_server_has_extra(tmp_path: Path):
    """server 有 ghost 持仓 (QMT 已经没了) → server_only > 0。"""
    sf = _factory(tmp_path)
    _seed_instance(sf, "inst", 500_000.0, {
        "600519.SH": 100,
        "601778.SH": 2300,   # 这只 QMT 没了
    })
    svc = ReconcileService(sf)

    snap = _snapshot("inst", 516_000.0, {"600519.SH": 100})  # QMT 多了 16K cash（卖了 601778）
    result = svc.reconcile(snap)

    assert result.cash_diff == 16_000.0
    assert result.n_matched == 1
    assert result.n_server_only == 1
    assert result.n_qmt_only == 0
    assert result.n_mismatched == 0
    assert len(result.diffs) == 1
    assert result.diffs[0].symbol == "601778.SH"
    assert result.diffs[0].server_qty == 2300
    assert result.diffs[0].qmt_qty == 0


def test_reconcile_dryrun_qmt_has_extra(tmp_path: Path):
    """QMT 有 server 没有的持仓 → qmt_only > 0。"""
    sf = _factory(tmp_path)
    _seed_instance(sf, "inst", 1_000_000.0, {"600519.SH": 100})
    svc = ReconcileService(sf)

    snap = _snapshot("inst", 985_000.0, {
        "600519.SH": 100,
        "000001.SZ": 1500,   # QMT 多了这只，server 不知道
    })
    result = svc.reconcile(snap)

    assert result.n_qmt_only == 1
    assert result.diffs[0].symbol == "000001.SZ"
    assert result.diffs[0].server_qty == 0
    assert result.diffs[0].qmt_qty == 1500


def test_reconcile_dryrun_qty_mismatch(tmp_path: Path):
    """同一只票，server 和 QMT 数量不同 → mismatched。"""
    sf = _factory(tmp_path)
    _seed_instance(sf, "inst", 1_000_000.0, {"600519.SH": 100})
    svc = ReconcileService(sf)

    snap = _snapshot("inst", 1_000_000.0, {"600519.SH": 200})
    result = svc.reconcile(snap)

    assert result.n_mismatched == 1
    assert result.n_server_only == 0
    assert result.n_qmt_only == 0
    assert result.diffs[0].symbol == "600519.SH"
    assert result.diffs[0].server_qty == 100
    assert result.diffs[0].qmt_qty == 200
    assert result.diffs[0].diff == 100


def test_reconcile_apply_changes_state(tmp_path: Path):
    """dry_run=False → instance_state 真的被改成 QMT 状态。"""
    sf = _factory(tmp_path)
    _seed_instance(sf, "inst", 500_000.0, {
        "600519.SH": 100,
        "601778.SH": 2300,
    })
    svc = ReconcileService(sf)

    snap = _snapshot("inst", 516_000.0, {"600519.SH": 100}, dry_run=False)
    result = svc.reconcile(snap)

    assert result.applied is True
    assert result.diffs == []   # apply 模式不返回 diff 详情（避免日志爆炸）

    # state 已改
    with sf() as s:
        inst = s.get(InstanceState, "inst")
        assert inst.virtual_cash == 516_000.0
        assert inst.virtual_positions == {"600519.SH": 100}


def test_reconcile_apply_ignores_zero_qty(tmp_path: Path):
    """QMT 推上来的 qty=0 应该被过滤掉（不要 store ghost key）。"""
    sf = _factory(tmp_path)
    _seed_instance(sf, "inst", 1_000.0, {})
    svc = ReconcileService(sf)

    snap = _snapshot("inst", 1_000.0, {
        "600519.SH": 100,
        "000001.SZ": 0,    # ← 应该被过滤
    }, dry_run=False)
    result = svc.reconcile(snap)

    assert result.applied is True
    with sf() as s:
        inst = s.get(InstanceState, "inst")
        assert inst.virtual_positions == {"600519.SH": 100}
        # 000001.SZ 不应该在 positions 里
        assert "000001.SZ" not in inst.virtual_positions


def test_reconcile_dryrun_returns_diffs_apply_omits(tmp_path: Path):
    """dry_run 返回 diff 详情；apply 不返回（避免日志爆炸）。"""
    sf = _factory(tmp_path)
    _seed_instance(sf, "inst", 1000.0, {"A": 100, "B": 200, "C": 300})
    svc = ReconcileService(sf)

    # dry-run: 改一个 mismatch + 一个 server-only + 一个 qmt-only
    snap = _snapshot("inst", 1000.0, {"A": 100, "B": 250, "D": 400}, dry_run=True)
    result = svc.reconcile(snap)
    assert len(result.diffs) == 3   # B mismatch, C server-only, D qmt-only

    # apply 同样改动：diffs 应该是空
    snap_apply = _snapshot("inst", 1000.0, {"A": 100, "B": 250, "D": 400}, dry_run=False)
    result_apply = svc.reconcile(snap_apply)
    assert result_apply.applied is True
    assert result_apply.diffs == []
    # 但计数还在
    assert result_apply.n_mismatched + result_apply.n_server_only + result_apply.n_qmt_only == 3


def test_reconcile_diffs_sorted_alphabetically(tmp_path: Path):
    """diffs 按 symbol 排序，方便人读。"""
    sf = _factory(tmp_path)
    _seed_instance(sf, "inst", 1000.0, {"Z": 100, "A": 100, "M": 100})
    svc = ReconcileService(sf)

    snap = _snapshot("inst", 1000.0, {})  # 全部清掉
    result = svc.reconcile(snap)
    assert [d.symbol for d in result.diffs] == ["A", "M", "Z"]
