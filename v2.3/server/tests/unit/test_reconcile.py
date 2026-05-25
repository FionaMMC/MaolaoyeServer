"""ReconcileService 测试"""
from datetime import datetime
from pathlib import Path

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState
from app.schemas.reconcile import QmtPositionSnapshot
from app.services.reconcile import (
    InstanceNotFound,
    OwnershipOverlap,
    ReconcileService,
)


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
    """dry_run=False → virtual_positions 对齐 QMT，virtual_cash 不变（由 settlement 维护）。"""
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

    # positions 已改；cash 不变（settlement 负责维护）
    with sf() as s:
        inst = s.get(InstanceState, "inst")
        assert inst.virtual_cash == 500_000.0     # cash 保持原值，不跟 QMT 对齐
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


# ── Sanity check guards (5/21 incident regression) ───────────────────────
def test_reconcile_filters_outlier_positions(tmp_path: Path):
    """QMT 模拟器默认股（qty > 100K）应该被过滤掉，不进 server。"""
    sf = _factory(tmp_path)
    _seed_instance(sf, "inst", 1_000_000.0, {})
    svc = ReconcileService(sf)

    snap = _snapshot("inst", 1_000_000.0, {
        "600519.SH": 100,           # 正常 V20H 持仓
        "000001.SZ": 10_000_000_000,  # 模拟器默认股 100 亿股 — 应被过滤
        "600028.SH": 999_999_999,    # 另一个异常大持仓
    }, dry_run=False)
    result = svc.reconcile(snap)

    # 过滤后 server 只该有正常那只
    with sf() as s:
        inst = s.get(InstanceState, "inst")
        assert inst.virtual_positions == {"600519.SH": 100}


def test_reconcile_rejects_huge_cash_deviation(tmp_path: Path):
    """cash 偏离检查已从 instance 级移除（改由 reconcile_cash_total 在 portfolio 级检查）。
    apply 模式不再因 cash 偏离而 raise；cash 不被覆写，positions 正常对齐。
    """
    sf = _factory(tmp_path)
    _seed_instance(sf, "inst", 1_000_000.0, {})  # server 当前 cash 100 万
    svc = ReconcileService(sf)

    # 以前会因 cash 偏离 188× 而 raise，现在不再 raise
    snap = _snapshot("inst", 188_000_000.0, {"600519.SH": 100}, dry_run=False)
    result = svc.reconcile(snap)
    assert result.applied is True

    # virtual_cash 不变（不被 QMT 的大 cash 覆写）
    with sf() as s:
        inst = s.get(InstanceState, "inst")
        assert inst.virtual_cash == 1_000_000.0
        assert inst.virtual_positions == {"600519.SH": 100}

    # dry_run 也正常
    snap_dry = _snapshot("inst", 188_000_000.0, {"600519.SH": 100}, dry_run=True)
    result = svc.reconcile(snap_dry)
    assert result.dry_run is True


def test_reconcile_accepts_small_cash_deviation(tmp_path: Path):
    """cash 偏离检查已移除；apply 正常执行，positions 对齐，cash 不变。"""
    sf = _factory(tmp_path)
    _seed_instance(sf, "inst", 1_000_000.0, {})
    svc = ReconcileService(sf)

    # 以前 8M 偏离（7×）会 raise，现在正常 apply
    snap = _snapshot("inst", 8_000_000.0, {"600519.SH": 100}, dry_run=False)
    result = svc.reconcile(snap)
    assert result.applied is True

    with sf() as s:
        inst = s.get(InstanceState, "inst")
        assert inst.virtual_cash == 1_000_000.0  # cash 不变
        assert inst.virtual_positions == {"600519.SH": 100}


def test_reconcile_initial_cash_param_used_as_baseline(tmp_path: Path):
    """initial_cash 参数已废弃（cash 偏离检查移除），传入不报错，apply 正常执行。"""
    sf = _factory(tmp_path)
    _seed_instance(sf, "inst", 100_000.0, {})
    svc = ReconcileService(sf)

    snap = _snapshot("inst", 9_000_000.0, {"600519.SH": 100}, dry_run=False)
    # 以前不传 initial_cash 会 raise；现在不再 raise
    result = svc.reconcile(snap)
    assert result.applied is True

    # 传 initial_cash 参数也不报错
    _seed_instance(sf, "inst2", 100_000.0, {})
    snap2 = _snapshot("inst2", 9_000_000.0, {"600519.SH": 100}, dry_run=False)
    result2 = svc.reconcile(snap2, initial_cash=10_000_000.0)
    assert result2.applied is True


# ── Task 17: owned_symbols 过滤 ───────────────────────────────────────────
def test_reconcile_whitelist_filter_v53_only_sees_etfs(tmp_path):
    sf = _factory(tmp_path)
    with sf() as s:
        s.add(InstanceState(
            instance_id="paper_v20h_v20h_v1_3",
            virtual_cash=10_000_000.0, virtual_positions={},
            owned_symbols=None, last_update=datetime.now().isoformat(),
        ))
        s.add(InstanceState(
            instance_id="paper_v53_v53",
            virtual_cash=10_000_000.0, virtual_positions={},
            owned_symbols=["510300.SH", "511260.SH", "518880.SH"],
            last_update=datetime.now().isoformat(),
        ))
        s.commit()

    svc = ReconcileService(sf)
    snap = _snapshot(
        "paper_v53_v53",
        10_000_000.0,
        {
            "600519.SH": 100,   # v20h's
            "000001.SZ": 200,   # v20h's
            "510300.SH": 5000,  # v53's
            "511260.SH": 70000, # v53's
        },
    )
    result = svc.reconcile(snap)
    # v53 视野里只有 510300 + 511260（518880 不在 qmt_positions 里），server 0 持仓 → 2 个 qmt_only
    assert result.n_qmt_only == 2
    assert result.n_server_only == 0


def test_reconcile_v20h_legacy_excludes_v53_etfs(tmp_path):
    """v20h reconcile 时不该把 v53 的 ETF 当成自己的"""
    sf = _factory(tmp_path)
    with sf() as s:
        s.add(InstanceState(
            instance_id="paper_v20h_v20h_v1_3",
            virtual_cash=10_000_000.0,
            virtual_positions={"600519.SH": 100},
            owned_symbols=None, last_update=datetime.now().isoformat(),
        ))
        s.add(InstanceState(
            instance_id="paper_v53_v53",
            virtual_cash=10_000_000.0,
            virtual_positions={"510300.SH": 5000},
            owned_symbols=["510300.SH", "511260.SH"],
            last_update=datetime.now().isoformat(),
        ))
        s.commit()

    svc = ReconcileService(sf)
    snap = _snapshot("paper_v20h_v20h_v1_3", 10_000_000.0,
                     {"600519.SH": 100, "510300.SH": 5000})
    result = svc.reconcile(snap)
    # v20h 只匹配 600519；510300 被 others_owned 过滤
    assert result.n_matched == 1
    assert result.n_qmt_only == 0
    assert result.n_mismatched == 0
    assert result.n_server_only == 0


def test_reconcile_apply_no_longer_overwrites_cash(tmp_path):
    """apply 模式：positions 强对齐，但 virtual_cash 不动"""
    sf = _factory(tmp_path)
    with sf() as s:
        s.add(InstanceState(
            instance_id="inst", virtual_cash=1_500_000.0, virtual_positions={},
            owned_symbols=None, last_update=datetime.now().isoformat(),
        ))
        s.commit()
    svc = ReconcileService(sf)
    snap = _snapshot("inst", 9_999_999.0, {"600519.SH": 100}, dry_run=False)
    result = svc.reconcile(snap)
    assert result.applied is True
    with sf() as s:
        inst = s.get(InstanceState, "inst")
        # virtual_cash 应仍是 1.5M（不变），positions 已更新
        assert float(inst.virtual_cash) == 1_500_000.0
        assert inst.virtual_positions == {"600519.SH": 100}


# ── Task 18: validate_no_overlap ──────────────────────────────────────────
def test_validate_no_overlap_raises_on_conflict(tmp_path):
    sf = _factory(tmp_path)
    with sf() as s:
        s.add(InstanceState(
            instance_id="inst_a", virtual_cash=0, virtual_positions={},
            owned_symbols=["510300.SH"], last_update=datetime.now().isoformat(),
        ))
        s.add(InstanceState(
            instance_id="inst_b", virtual_cash=0, virtual_positions={},
            owned_symbols=["510300.SH", "511260.SH"],
            last_update=datetime.now().isoformat(),
        ))
        s.commit()
    svc = ReconcileService(sf)
    with pytest.raises(OwnershipOverlap) as exc:
        svc.validate_no_overlap()
    assert "510300.SH" in str(exc.value)


def test_validate_no_overlap_ok_when_disjoint(tmp_path):
    sf = _factory(tmp_path)
    with sf() as s:
        s.add(InstanceState(
            instance_id="inst_a", virtual_cash=0, virtual_positions={},
            owned_symbols=["510300.SH"], last_update=datetime.now().isoformat(),
        ))
        s.add(InstanceState(
            instance_id="inst_b", virtual_cash=0, virtual_positions={},
            owned_symbols=["159915.SZ"], last_update=datetime.now().isoformat(),
        ))
        s.add(InstanceState(
            instance_id="legacy", virtual_cash=0, virtual_positions={},
            owned_symbols=None, last_update=datetime.now().isoformat(),
        ))
        s.commit()
    svc = ReconcileService(sf)
    svc.validate_no_overlap()  # 无 raise


# ── Task 19: reconcile_cash_total ─────────────────────────────────────────
def test_reconcile_cash_total_within_tolerance(tmp_path):
    sf = _factory(tmp_path)
    with sf() as s:
        s.add(InstanceState(instance_id="a", virtual_cash=5_000_000,
                            virtual_positions={}, last_update=datetime.now().isoformat()))
        s.add(InstanceState(instance_id="b", virtual_cash=5_000_000,
                            virtual_positions={}, last_update=datetime.now().isoformat()))
        s.commit()
    svc = ReconcileService(sf)
    assert svc.reconcile_cash_total(qmt_total_cash=10_100_000.0, tolerance=0.05) is True


def test_reconcile_cash_total_alarm_on_big_deviation(tmp_path, caplog):
    import logging
    sf = _factory(tmp_path)
    with sf() as s:
        s.add(InstanceState(instance_id="a", virtual_cash=10_000_000,
                            virtual_positions={}, last_update=datetime.now().isoformat()))
        s.commit()
    svc = ReconcileService(sf)
    caplog.set_level(logging.WARNING, logger="app.services.reconcile")
    # qmt_total = 5M vs virtual 10M → 50% deviation > 5% tolerance
    result = svc.reconcile_cash_total(qmt_total_cash=5_000_000.0, tolerance=0.05)
    assert result is False
    assert any("cash_total mismatch" in r.getMessage() for r in caplog.records)
