# 多租户子账户台账 — 地基 Implementation Plan (Plan 2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把共享 QMT 账户的对账从"按 symbol 白名单归属"演进为"台账总量对账"（`QMT[X] == Σ_i books_i[X]`），并退掉 v20h legacy claim-all —— 全程先影子验证再切权，绝不在未证明等价前让新逻辑对 v20h/v53 有权。

**Architecture:** 新增 portfolio 级 `reconcile_total()`（只校验每个 symbol 的台账之和 == QMT 聚合持仓，报警不改账）。先与现有 per-instance `reconcile()` **影子并行**跑，断言对 v20h+v53（universe 不相交）逐 symbol 结论一致。等价证明后，v20h legacy 退成显式台账（一次性 seed 快照 + 独立验证门）。归属始终靠下单血缘（`OrderSignalMap`），不靠事后认领。

**Tech Stack:** Python 3 / SQLAlchemy / SQLite (`pipeline-server.db`) / FastAPI / pytest。改动集中在 `v2.3/server/app/services/reconcile.py` + scheduler + 一个一次性 migration 脚本。

**分支基线:** `feature/v79-multitenant-ledger`（已 rebase 到 master `de8e242` = 部署版）。

---

## 前置事实（实现者必读）

- `InstanceState`（`app/models/instance_state.py`）字段：`instance_id, virtual_cash, virtual_positions(JSON dict), last_update, strategy_state, owned_symbols(JSON list|None)`。**无需新 migration**——字段都在。
- 现 `reconcile(snapshot, ...)`（`app/services/reconcile.py:78`）是 **per-instance**：client 对每个 instance 推同一份全账户 QMT 快照，方法按 `owned_symbols` 过滤（白名单 / legacy「减去他人 owned」），`dry_run=False` 时把该 instance 的 `virtual_positions` 强制对齐到过滤后的 QMT 持仓。
- `reconcile_cash_total(qmt_total_cash, tolerance=0.05)`（同文件:257）已是 portfolio 级 cash 校验（报警不改账）。**新方法仿它的形状**做 positions 版。
- 归属：每张聚合单归唯一 instance（`aggregate.py` 按 `account_group` 聚合），`OrderSignalMap` 记 order→signal→instance，settlement 按 order_id 回写对应 instance 的 `virtual_positions`。总量对账不改这条链。
- 现役 instance：`paper_v20h_v20h_v1_3`（`owned_symbols=None` legacy）、`paper_v53_v53`（10 ETF 白名单）。二者 universe 不相交 → **新旧模型对它们必须逐 symbol 等价**，这是影子验证的断言基础。

## File Structure

- **Modify** `v2.3/server/app/services/reconcile.py` — 加 `reconcile_total()` + `shadow_compare()`；不动现有 `reconcile()`（切权前它仍是权威）。
- **Modify** `v2.3/server/app/schemas/reconcile.py` — 加 `TotalReconcileResult` dataclass。
- **Create** `v2.3/server/tests/unit/test_reconcile_total.py` — 总量对账单测。
- **Create** `v2.3/server/tests/unit/test_reconcile_shadow_equiv.py` — 影子等价性单测（v20h+v53 场景）。
- **Modify** `v2.3/server/app/scheduler/pipeline.py` 或 settlement 后钩子 — 每日结算后调 `reconcile_total()`（**shadow：只 log/alert，不改账**）。
- **Create** `v2.3/server/scripts/seed_v20h_ledger.py` — 一次性把 v20h legacy 退成显式台账（M0a，独立门）。
- **Create** `v2.3/server/tests/unit/test_seed_v20h_ledger.py` — seed 逻辑单测。

---

## Task 1: `TotalReconcileResult` schema

**Files:**
- Modify: `v2.3/server/app/schemas/reconcile.py`
- Test: `v2.3/server/tests/unit/test_reconcile_total.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_reconcile_total.py
from app.schemas.reconcile import TotalReconcileResult

def test_total_reconcile_result_fields():
    r = TotalReconcileResult(
        snapshot_time="2026-07-06T16:00:00+08:00",
        n_symbols=3, n_matched=2, n_mismatched=1,
        mismatches=[{"symbol": "511260.SH", "qmt": 13000, "ledger_sum": 10000, "diff": 3000,
                     "per_instance": {"paper_v53_v53": 10000}}],
        cash_ok=True, ledger_cash_total=20000000.0, qmt_cash=20000000.0,
    )
    assert r.n_mismatched == 1
    assert r.mismatches[0]["diff"] == 3000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd v2.3/server && python -m pytest tests/unit/test_reconcile_total.py::test_total_reconcile_result_fields -v`
Expected: FAIL — `ImportError: cannot import name 'TotalReconcileResult'`

- [ ] **Step 3: 加 dataclass**

```python
# app/schemas/reconcile.py — 在文件末尾追加
from dataclasses import dataclass, field

@dataclass
class TotalReconcileResult:
    """Portfolio 级总量对账结果：Σ_instances virtual_positions[X] vs QMT[X]。"""
    snapshot_time: str
    n_symbols: int
    n_matched: int
    n_mismatched: int
    mismatches: list[dict]        # [{symbol, qmt, ledger_sum, diff, per_instance:{iid:qty}}]
    cash_ok: bool
    ledger_cash_total: float
    qmt_cash: float
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd v2.3/server && python -m pytest tests/unit/test_reconcile_total.py::test_total_reconcile_result_fields -v`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add v2.3/server/app/schemas/reconcile.py v2.3/server/tests/unit/test_reconcile_total.py
git commit -m "feat(reconcile): TotalReconcileResult schema for total-sum reconcile"
```

---

## Task 2: `reconcile_total()` — 逐 symbol 台账之和 vs QMT

**Files:**
- Modify: `v2.3/server/app/services/reconcile.py`
- Test: `v2.3/server/tests/unit/test_reconcile_total.py`

- [ ] **Step 1: 写失败测试**（覆盖：匹配、重叠 ETF 之和匹配、不匹配报警、不改账）

```python
# tests/unit/test_reconcile_total.py — 追加
import pytest
from app.services.reconcile import ReconcileService
from app.models import InstanceState

def _mk(session_factory, iid, cash, pos, owned=None):
    with session_factory() as s:
        s.add(InstanceState(instance_id=iid, virtual_cash=cash, virtual_positions=pos,
                            last_update="t", owned_symbols=owned))
        s.commit()

def test_total_reconcile_overlap_sum_matches(session_factory):
    # v53 持 10000 511260；v79 防御持 3000 511260 → 台账和 13000 == QMT 13000 → 匹配
    _mk(session_factory, "paper_v53_v53", 10_000_000, {"511260.SH": 10000}, owned=["511260.SH"])
    _mk(session_factory, "paper_v79_v79_relay", 10_000_000, {"511260.SH": 3000}, owned=["511260.SH"])
    svc = ReconcileService(session_factory)
    r = svc.reconcile_total(
        qmt_positions={"511260.SH": 13000}, qmt_cash=20_000_000,
        snapshot_time="t",
    )
    assert r.n_mismatched == 0
    assert r.n_matched == 1

def test_total_reconcile_mismatch_alerts_no_write(session_factory):
    _mk(session_factory, "paper_v53_v53", 10_000_000, {"511260.SH": 10000}, owned=["511260.SH"])
    svc = ReconcileService(session_factory)
    r = svc.reconcile_total(qmt_positions={"511260.SH": 12000}, qmt_cash=10_000_000, snapshot_time="t")
    assert r.n_mismatched == 1
    assert r.mismatches[0]["diff"] == 2000          # qmt - ledger_sum
    # 不改账：v53 台账仍是 10000
    with session_factory() as s:
        assert s.get(InstanceState, "paper_v53_v53").virtual_positions == {"511260.SH": 10000}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd v2.3/server && python -m pytest tests/unit/test_reconcile_total.py -v -k total_reconcile`
Expected: FAIL — `AttributeError: 'ReconcileService' object has no attribute 'reconcile_total'`

- [ ] **Step 3: 实现 `reconcile_total`**

```python
# app/services/reconcile.py — 在 ReconcileService 内、reconcile_cash_total 之后追加
    def reconcile_total(
        self,
        qmt_positions: dict[str, int],
        qmt_cash: float,
        snapshot_time: str,
        cash_tolerance: float = 0.05,
    ) -> "TotalReconcileResult":
        """Portfolio 级总量对账：对每个 symbol 断言 Σ_i virtual_positions[X] == QMT[X]。
        只报警、绝不改账（归属由 settlement 血缘维护）。多个 instance 可共同持有同一
        symbol（如 v53 + v79 都持 511260.SH），本方法只校验其和。
        """
        from app.schemas.reconcile import TotalReconcileResult  # 局部 import 避免环
        with self.session_factory() as session:
            instances = session.execute(select(InstanceState)).scalars().all()
            ledger_sum: dict[str, int] = {}
            per_instance: dict[str, dict[str, int]] = {}
            cash_total = 0.0
            for inst in instances:
                cash_total += float(inst.virtual_cash)
                for s, q in (inst.virtual_positions or {}).items():
                    qi = int(q)
                    if qi <= 0:
                        continue
                    ledger_sum[s] = ledger_sum.get(s, 0) + qi
                    per_instance.setdefault(s, {})[inst.instance_id] = qi

        qmt = {s: int(q) for s, q in qmt_positions.items()
               if int(q) > 0 and int(q) <= MAX_REASONABLE_QTY_PER_STOCK}
        all_syms = set(ledger_sum) | set(qmt)
        matched = 0
        mismatches: list[dict] = []
        for s in sorted(all_syms):
            lg = ledger_sum.get(s, 0)
            qq = qmt.get(s, 0)
            if lg == qq:
                matched += 1
            else:
                mismatches.append({
                    "symbol": s, "qmt": qq, "ledger_sum": lg, "diff": qq - lg,
                    "per_instance": per_instance.get(s, {}),
                })

        cash_ok = True
        if cash_total > 0:
            cash_ok = abs(qmt_cash - cash_total) / cash_total <= cash_tolerance

        result = TotalReconcileResult(
            snapshot_time=snapshot_time, n_symbols=len(all_syms),
            n_matched=matched, n_mismatched=len(mismatches),
            mismatches=mismatches, cash_ok=cash_ok,
            ledger_cash_total=cash_total, qmt_cash=float(qmt_cash),
        )
        if mismatches or not cash_ok:
            logger.error(
                "reconcile_total MISMATCH: %d/%d symbols off, cash_ok=%s. mismatches=%s",
                len(mismatches), len(all_syms), cash_ok, mismatches[:10],
            )
        else:
            logger.info("reconcile_total OK: %d symbols, ledger==QMT, cash within tol", len(all_syms))
        return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd v2.3/server && python -m pytest tests/unit/test_reconcile_total.py -v -k total_reconcile`
Expected: PASS (2 passed)

- [ ] **Step 5: commit**

```bash
git add v2.3/server/app/services/reconcile.py v2.3/server/tests/unit/test_reconcile_total.py
git commit -m "feat(reconcile): reconcile_total — per-symbol ledger-sum vs QMT, alert-only"
```

---

## Task 3: 影子等价性 —— 断言新旧模型对 v20h+v53 逐 symbol 一致

**Files:**
- Modify: `v2.3/server/app/services/reconcile.py` (加 `shadow_compare()`)
- Test: `v2.3/server/tests/unit/test_reconcile_shadow_equiv.py`

**原理:** v20h(legacy) + v53(白名单) universe 不相交。给定一份全账户 QMT 快照，"每个 instance 旧 reconcile(dry_run) 过滤后的持仓之并" 必须等于 "reconcile_total 看到的 QMT 端每 symbol 值"，且台账之和（settlement 维护的 virtual_positions）此刻也应与 QMT 一致。`shadow_compare` 返回两模型是否一致，供切权前 N 天断言。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_reconcile_shadow_equiv.py
from app.services.reconcile import ReconcileService
from app.models import InstanceState

def _seed(sf, iid, pos, owned):
    with sf() as s:
        s.add(InstanceState(instance_id=iid, virtual_cash=10_000_000,
                            virtual_positions=pos, last_update="t", owned_symbols=owned))
        s.commit()

def test_shadow_equiv_disjoint_universes(session_factory):
    # v20h 持 2 股，v53 持 2 ETF；QMT = 两者并集，且各自 virtual==真实
    _seed(session_factory, "paper_v20h_v20h_v1_3", {"600353.SH": 100, "000727.SZ": 100}, None)
    _seed(session_factory, "paper_v53_v53", {"511260.SH": 10000, "518880.SH": 5000},
          ["511260.SH", "518880.SH"])
    qmt = {"600353.SH": 100, "000727.SZ": 100, "511260.SH": 10000, "518880.SH": 5000}
    svc = ReconcileService(session_factory)
    report = svc.shadow_compare(qmt_positions=qmt, qmt_cash=20_000_000, snapshot_time="t")
    assert report["consistent"] is True
    assert report["total"].n_mismatched == 0

def test_shadow_flags_when_ledger_drifts(session_factory):
    _seed(session_factory, "paper_v53_v53", {"511260.SH": 9000}, ["511260.SH"])  # 台账少 1000
    svc = ReconcileService(session_factory)
    report = svc.shadow_compare(qmt_positions={"511260.SH": 10000}, qmt_cash=10_000_000, snapshot_time="t")
    assert report["consistent"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd v2.3/server && python -m pytest tests/unit/test_reconcile_shadow_equiv.py -v`
Expected: FAIL — `AttributeError: ... 'shadow_compare'`

- [ ] **Step 3: 实现 `shadow_compare`**

```python
# app/services/reconcile.py — ReconcileService 内追加
    def shadow_compare(self, qmt_positions: dict[str, int], qmt_cash: float,
                       snapshot_time: str) -> dict:
        """影子对比：跑 reconcile_total，并断言当前台账之和逐 symbol == QMT。
        返回 {consistent: bool, total: TotalReconcileResult}。切权前每日 log 此结果，
        consistent 连续 N 天为 True 才允许把 total 变成权威。
        """
        total = self.reconcile_total(qmt_positions, qmt_cash, snapshot_time)
        consistent = (total.n_mismatched == 0 and total.cash_ok)
        logger.info("shadow_compare: consistent=%s (mismatched=%d cash_ok=%s)",
                    consistent, total.n_mismatched, total.cash_ok)
        return {"consistent": consistent, "total": total}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd v2.3/server && python -m pytest tests/unit/test_reconcile_shadow_equiv.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: commit**

```bash
git add v2.3/server/app/services/reconcile.py v2.3/server/tests/unit/test_reconcile_shadow_equiv.py
git commit -m "feat(reconcile): shadow_compare — daily total-vs-QMT consistency probe"
```

---

## Task 4: 每日结算后跑影子对账（log-only，零权威）

**Files:**
- Modify: `v2.3/server/app/scheduler/pipeline.py`（找到 settlement/reconcile 每日钩子；若无独立 cron，则在 trade-result 落库后调用）
- Test: `v2.3/server/tests/integration/test_shadow_hook.py`

**注意:** 实现者先 `grep -n "reconcile_cash_total\|def settle\|scheduler" app/scheduler/*.py app/api/trade_result.py` 定位现有每日 cash-total 调用点，把 `shadow_compare` 挂在同一处（同一份 QMT 快照）。**只 log/alert，不写库、不改任何 instance。**

- [ ] **Step 1: 写失败集成测试** —— 触发结算后，日志/返回里应出现 shadow_compare 结果，且 instance_state 无变化。（实现者按现有 settlement 测试夹具补全断言：`caplog` 里有 `shadow_compare: consistent=`，且 `virtual_positions` 前后相等。）
- [ ] **Step 2: 跑测试确认失败**（钩子未接 → 无 log）
- [ ] **Step 3: 在 cash-total 校验同点追加 `self.reconcile.shadow_compare(qmt_positions=snapshot.qmt_positions, qmt_cash=snapshot.qmt_cash, snapshot_time=snapshot.snapshot_time)`；把 `consistent=False` 接入现有微信 alert 通道（与 cash_total mismatch 同渠道）。**不加任何写库路径。****
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: commit** — `feat(reconcile): daily shadow total-reconcile hook (log/alert only)`

---

## Task 5: 部署影子 + 观察窗（无代码，运维门）

- [ ] 把本分支合并/部署到 `/opt/qmt-server`（`git pull` + `systemctl restart qmt-server`）。**此刻新代码只多跑一个 log-only 的 shadow_compare，`reconcile()` 仍是唯一权威 → v20h/v53 零行为变化。**
- [ ] 连续观察 **≥3 个交易日**（含至少一次 v53 月末调仓若赶上）：每日 `journalctl -u qmt-server | grep shadow_compare` 应恒为 `consistent=True`。
- [ ] 若任何一天 `consistent=False`：说明 settlement 维护的台账与 QMT 已有漂移（**先修漂移**，不推进）。查 `mismatches` 里是哪个 symbol / 哪个 instance。
- [ ] **门槛：连续 3 日 consistent=True 才进 Task 6。**

---

## Task 6: v20h legacy 退成显式台账（M0a — 最高风险单步，独立门）

**Files:**
- Create: `v2.3/server/scripts/seed_v20h_ledger.py`
- Test: `v2.3/server/tests/unit/test_seed_v20h_ledger.py`

**原理:** v20h `owned_symbols=None`（legacy）。退 legacy = ①确保 v20h 的 `virtual_positions` 已是其真实持仓（settlement 一直在维护，Task 5 影子已证 consistent）；②把 `owned_symbols` 从 `None` 改成 v20h 当前实际持有的 symbol 列表快照（使它不再"认领他人之外的一切"，只认自己已持有的）。此后新买入的 symbol 由 settlement 血缘自然并入其台账；总量对账保证不串账。

- [ ] **Step 1: 写失败测试** —— `seed_v20h_ledger(session_factory, instance_id, dry_run=True)` 返回将写入的 `owned_symbols`（= 当前 `virtual_positions` 的 keys），`dry_run=True` 时不改库；`dry_run=False` 时把该 instance `owned_symbols` 设为该列表。断言 legacy(None) → 显式 list 后，其它 instance 的 legacy 计算不再依赖它。

```python
# tests/unit/test_seed_v20h_ledger.py
from scripts.seed_v20h_ledger import seed_v20h_ledger
from app.models import InstanceState

def test_seed_writes_current_holdings_as_whitelist(session_factory):
    with session_factory() as s:
        s.add(InstanceState(instance_id="paper_v20h_v20h_v1_3", virtual_cash=10_000_000,
                            virtual_positions={"600353.SH": 100, "000727.SZ": 100},
                            last_update="t", owned_symbols=None))
        s.commit()
    planned = seed_v20h_ledger(session_factory, "paper_v20h_v20h_v1_3", dry_run=True)
    assert set(planned) == {"600353.SH", "000727.SZ"}
    with session_factory() as s:  # dry_run 不改
        assert s.get(InstanceState, "paper_v20h_v20h_v1_3").owned_symbols is None
    seed_v20h_ledger(session_factory, "paper_v20h_v20h_v1_3", dry_run=False)
    with session_factory() as s:
        assert set(s.get(InstanceState, "paper_v20h_v20h_v1_3").owned_symbols) == {"600353.SH", "000727.SZ"}
```

- [ ] **Step 2: 跑确认失败** — `ModuleNotFoundError: scripts.seed_v20h_ledger`
- [ ] **Step 3: 实现**

```python
# scripts/seed_v20h_ledger.py
"""一次性：把 v20h 从 legacy(owned_symbols=None) 退成显式台账。
用法: python -m scripts.seed_v20h_ledger --instance paper_v20h_v20h_v1_3 [--apply]
默认 dry-run，只打印将写入的 owned_symbols。"""
from __future__ import annotations
from sqlalchemy import select
from app.models import InstanceState

def seed_v20h_ledger(session_factory, instance_id: str, dry_run: bool = True) -> list[str]:
    with session_factory() as s:
        inst = s.get(InstanceState, instance_id)
        if inst is None:
            raise ValueError(f"instance {instance_id} not found")
        planned = sorted(k for k, q in (inst.virtual_positions or {}).items() if int(q) > 0)
        if not dry_run:
            inst.owned_symbols = planned
            s.commit()
        return planned

if __name__ == "__main__":
    import argparse
    from app.db import session_factory  # 实现者核对实际 session_factory import 路径
    p = argparse.ArgumentParser()
    p.add_argument("--instance", required=True)
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    out = seed_v20h_ledger(session_factory, a.instance, dry_run=not a.apply)
    print(("APPLIED" if a.apply else "DRY-RUN"), "owned_symbols =", out)
```

- [ ] **Step 4: 跑确认通过**
- [ ] **Step 5: commit** — `feat(reconcile): seed_v20h_ledger — retire legacy into explicit whitelist`
- [ ] **Step 6（运维门，人工）:** 生产上先 `--` dry-run 打印 v20h 当前 owned；人工核对 == QMT 里属于 v20h 的股票（此时账户 = v20h 股 + v53 ETF，可精确核）；`--apply`；再连续 ≥2 日确认 `reconcile()`（旧）与 `reconcile_total()`（影子）对 v20h 仍逐 symbol 一致。**通过后 legacy 已退，地基完成。**

---

## Task 7: 收尾 —— 全量测试 + 文档

- [ ] `cd v2.3/server && python -m pytest tests/ -q` 全绿。
- [ ] 更新 `docs/V20H_OPERATIONS_HANDBOOK.md` + `V53_OPERATIONS_HANDBOOK.md`：对账模型改为总量对账，排错看 `reconcile_total mismatch` 日志（含 per_instance 拆分）。
- [ ] commit — `docs: total-sum reconcile ops notes`

---

## Self-Review

- **Spec 覆盖**：Part A 三改造 —— ①总量对账=Task 1-4；②退 v20h legacy=Task 6；③预算纪律=已有 `reconcile_cash_total`（Task 2 把 cash 校验并入 total）+ 运维核对 ¥30M（属 Plan 3 go-live）。✅
- **切权安全**：新 `reconcile_total`/`shadow_compare` 全程 log-only（Task 2-5），旧 `reconcile()` 保持权威直到 Task 6 门通过 → v20h/v53 在未证明等价前零行为变化。✅
- **占位符**：核心新方法 `reconcile_total`/`shadow_compare`/`seed_v20h_ledger` 均给完整代码；Task 4/5/6 的运维/集成步依赖现有夹具与 import 路径，已标注实现者需 `grep` 核对的具体点（session_factory import、每日 cash-total 钩子位置）。
- **类型一致**：`TotalReconcileResult` 字段在 Task 1 定义，Task 2/3 使用一致。

## Execution Handoff

本 Plan 完成后 → Plan 3（v79 go-live：flip dry_run、¥30M 注资核对、首建眼检）。Plan 1（v79 relay dry-run）与本 Plan 独立、可并行。
