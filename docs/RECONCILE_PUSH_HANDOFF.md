# Windows 端：每日账户对账推送 恢复运行（一键 handoff）

> **目标**：让 Windows 客户端每个交易日收盘结算后，把 **QMT 真实账户快照（现金 + 持仓）** POST 给服务器对账。这条链**从 ~2026-07-02 起停了**，需要恢复。
> **读者**：接手运行的同伴。你在 7/2 之前跑过这个脚本，这份是"契约 + 恢复指南"；脚本还在就直接重启，丢了就照 §3 重建。
> **紧急度**：高。停了 4 天 = ①v20h/v53 的虚拟账本已 4 天没跟真实账户核对（可能悄悄脱节）；②v79 上线前的"影子对账"必须靠它才能开始跑。

---

## 0. 为什么现在很重要

- **账本健康**：服务器给每个策略维护一套虚拟持仓，靠"成交回报 + 每日对账"跟 QMT 真账户保持一致。对账一停，成交要是漏推/错推，账本就悄悄和真实账户脱节，且**没有任何告警**（这正是之前踩过的坑）。
- **v79 上线的第一道门**：v79 要上模拟盘，前置是把服务器对账逻辑升级成"总量对账"并**影子跑 3 天证明无误**。而影子对账**只在收到账户快照时才运行**——你不推，它一次都不跑，3 天时钟永远是 0。所以**恢复推送 = 解锁 v79 的第一步**。

服务器侧已经准备好：每次收到你的推送，都会自动多跑一次影子校验（只记日志、不动任何东西）。你只管把推送恢复。

---

## 1. 做什么（一句话）

**每交易日收盘 + 成交结算完成后**，从 QMT 拉一次账户 **可用现金 + 全部持仓**，然后对**每个 instance** POST 一次给服务器。当前两个 instance：

- `paper_v20h_v20h_v1_3`
- `paper_v53_v53`

（`paper_v79_*` 暂不用推——它还没建仓；等 v79 正式上线再加。）

> 注意：QMT 只有一个账户、一份持仓。你对两个 instance 推的是**同一份**账户快照（相同的 `qmt_cash` / `qmt_positions`），只是 `instance_id` 不同。服务器会各自按白名单过滤。

---

## 2. 服务器契约（精确）

**Endpoint**：`POST http://120.26.138.82:8000/admin/reconcile-positions`
**Header**：`Authorization: Bearer pipeline-v23-shared-secret-2026`、`Content-Type: application/json`
**Body**（JSON，字段全部必填除 dry_run/force）：

```json
{
  "instance_id":   "paper_v20h_v20h_v1_3",
  "qmt_account_id":"301300148788",
  "qmt_cash":      922928.00,
  "qmt_positions": {"600519.SH": 100, "000001.SZ": 200, "...": 0},
  "snapshot_time": "2026-07-07T16:00:00+08:00",
  "dry_run":       false
}
```

- `qmt_cash`：账户**可用资金**（元，float）。
- `qmt_positions`：`{QMT标准code: 持仓股数}`，code 形如 `600519.SH`/`000001.SZ`（6位+交易所后缀，和你篮子里一样）。数量是**股**不是手。
- `snapshot_time`：你 dump 的时间，ISO 格式。
- `dry_run`：
  - `true` → 只返回 diff 报告，**不改**服务器账本（先看差多少，安全）。
  - `false` → **apply**：把该 instance 的虚拟持仓强制对齐到 QMT（保持账本同步）。
- 返回 `code:0 message:ok` + diff 汇总（matched / mismatched / server_only / qmt_only）。

---

## 3. 恢复脚本 `push_reconcile.py`（丢了就照这个重建）

放 Windows 客户端侧。核心就是"拉 QMT 快照 → POST 两次"。QMT 拉持仓/现金那段用你**7/2 之前已有的代码**即可；下面给出组包 + 上传的骨架。

```python
"""push_reconcile.py — 每交易日收盘结算后：QMT 账户快照 → server 对账。
用法: python push_reconcile.py [--dry-run]   默认 apply(dry_run=false)
"""
import argparse, datetime as dt, requests

BASE = "http://120.26.138.82:8000"
KEY  = "pipeline-v23-shared-secret-2026"
ACCOUNT = "301300148788"
INSTANCES = ["paper_v20h_v20h_v1_3", "paper_v53_v53"]   # v79 暂不推

def pull_qmt_snapshot():
    """【CONFIRM 用你 7/2 前已有的 QMT 查询代码】
    返回 (cash: float, positions: dict[str,int])。
    典型 xtquant.xttrade:
        asset = xt_trader.query_stock_asset(acc)         # asset.cash 可用资金
        poss  = xt_trader.query_stock_positions(acc)     # 每项 .stock_code / .volume
    组成: positions = {p.stock_code: int(p.volume) for p in poss if p.volume > 0}
    注意 code 要 QMT 标准格式(600519.SH)，volume 单位是股。
    """
    raise NotImplementedError("接上你已有的 QMT 查询逻辑")

def push(instance_id, cash, positions, dry_run):
    payload = {
        "instance_id": instance_id,
        "qmt_account_id": ACCOUNT,
        "qmt_cash": float(cash),
        "qmt_positions": {k: int(v) for k, v in positions.items() if int(v) > 0},
        "snapshot_time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "dry_run": dry_run,
    }
    r = requests.post(f"{BASE}/admin/reconcile-positions",
                      headers={"Authorization": f"Bearer {KEY}"}, json=payload)
    print(instance_id, r.status_code, r.text[:300])

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cash, positions = pull_qmt_snapshot()
    for iid in INSTANCES:
        push(iid, cash, positions, dry_run=a.dry_run)
```

**一键 `run_reconcile.bat`**：
```bat
@echo off
cd /d %~dp0
.\venv\Scripts\python push_reconcile.py %*
```

---

## 4. 首次恢复的正确姿势（因为停了 4 天，先看再 apply）

停了 4 天，账本可能有漂移。**别一上来就 apply**，按这两步：

1. **先 dry-run 看差多少**：`run_reconcile.bat --dry-run`
   - 看返回里的 `mismatched` / `server_only` / `qmt_only`。
   - 若 `server_only` 很大（服务器有一堆 QMT 没有的持仓）→ 可能是快照没拉全，**先查 QMT 快照是否完整**，别急着 apply。
2. **差异合理 → 正式 apply**：`run_reconcile.bat`（dry_run=false）
   - 服务器有个护栏：如果一次 apply 会清掉 ≥34% 且 ≥3 个持仓，会返回 **422 拦截**（防止残缺快照误删好仓）。真遇到就是快照不全的信号——**查完整性，不要盲目加 force**。

之后每个交易日收盘结算后跑一次 `run_reconcile.bat` 即可（可挂 Windows 任务计划）。

---

## 5. 验收

1. `run_reconcile.bat --dry-run` → 两个 instance 都 `200 ok` + 打印 diff 汇总。
2. 正式 apply → `200 ok`。
3. 告诉服务器侧确认：日志里应出现每个 instance 的 `reconcile ...` + 一行 `shadow_compare: consistent=True/False`（后者是 v79 影子校验，说明时钟开始走了）。
4. 之后每交易日跑一次。**连续 3 个交易日 `consistent=True`** = v79 影子对账通过，可进下一步（退 legacy → v79 上线）。

---

## 6. 与其他两条链的关系（别搞混）

Windows 侧现在有 **3 条独立推送**，节奏不同，别混：

| 链 | 脚本 | 何时 | 作用 |
|---|---|---|---|
| v20h pred 刷新 | `v20h_refresh.py` | 每交易日**收盘后立即(15:00)** | 刷 v20h 信号 + 触发管线 |
| v79 篮子 | `produce_v79_basket.py` | **每周**一次 | 推 v79 目标篮子 |
| **账户对账（本文档）** | `push_reconcile.py` | 每交易日**结算完成后** | 账本对账 + v79 影子时钟 |
