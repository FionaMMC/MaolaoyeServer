# MiniQMT one-lot live canary runbook

## Scope and decision

This runbook proves the real MiniQMT broker path:

`live account fingerprint → live quote → FIX_PRICE order_stock → broker order query → status/cancel evidence`

It does **not** call the Hydra server or claim that the formal month-end target is
ready.  A server order cannot safely be sliced for this test: reporting only 100
units against a larger formal order would create a false `PARTIAL` batch and
block or contaminate the residual ledger.

The script can place a real-money order.  Use it only after an identified operator
and approver agree on the account, symbol, maximum loss, time window and treatment
of a filled 100-unit position.

## Hard controls

- real `mode=live` and `execution_domain=live` only;
- account ID checked against the configured private SHA-256 fingerprint;
- only `510300.SH` or `159915.SZ`;
- BUY only, exactly 100 units, limit order only;
- maximum notional CNY 2,000, including an additional cash/commission check;
- 09:35–11:25 or 13:05–14:50 China time only;
- QMT tick age at most 5 seconds, spread at most 15bps, premium at most 30bps;
- IOPV required: QMT tick value, or an explicit time-stamped operator source;
- plan expires after 120 seconds and the approved limit is never chased upward;
- `HYDRA_LIVE_TRADING_ENABLED` and `HYDRA_LIVE_CANARY_ENABLED` are independent;
- exact plan SHA-256 confirmation is required for submission;
- an immutable submit lock is written before `order_stock`; a crash is treated as
  possibly submitted and must never be retried;
- one canary order per real account per trading day;
- plan, submit lock and hash-chained events are mode `0600` under the live log dir.

## Before the window

1. MiniQMT is logged into the exact approved cash account and the client can query
   assets, positions, orders and live ticks.
2. The live and paper userdata paths, session IDs, state DBs, logs, account IDs and
   Windows task prefixes are distinct.
3. All Hydra server live generation and delivery switches remain off.  This canary
   does not need them.
4. `HYDRA_LIVE_TRADING_ENABLED=false` and
   `HYDRA_LIVE_CANARY_ENABLED=false`.
5. The account has no active/cancelable order and no earlier `hydra_canary` order
   on the same day.
6. The approver has selected one of the two canary symbols and accepted that a fill
   leaves a real 100-unit holding plus broker commission.

Run from the `v2.3` directory in the MiniQMT Windows environment.  The examples use
PowerShell and a path inside `HYDRA_LIVE_LOG_DIR`.

## Phase 1 — read-only immutable plan

```powershell
$env:PYTHONPATH = (Get-Location).Path
$env:HYDRA_LIVE_TRADING_ENABLED = "false"
$env:HYDRA_LIVE_CANARY_ENABLED = "false"
$CanaryPlan = "$env:HYDRA_LIVE_LOG_DIR\canary\plan-$(Get-Date -Format yyyyMMdd-HHmmss).json"
python -m live_client.canary plan --symbol 510300.SH --output $CanaryPlan
```

If the native tick does not expose IOPV, the command fails closed.  Obtain a
current, time-stamped IOPV from the approved MiniQMT/broker display and rerun:

```powershell
python -m live_client.canary plan --symbol 510300.SH --output $CanaryPlan `
  --iopv 4.0123 --iopv-source "MiniQMT UI 2026-09-01 09:45:05 CST"
```

Do not reuse the same filename after any failure.  Inspect the JSON and verify:

- account alias and fingerprint;
- symbol, `quantity=100`, `direction=BUY`, limit price and notional;
- quote time/age, bid/ask spread, IOPV source and premium;
- expiry time and `plan_sha256`.

## Phase 2 — one real submission

The plan expires after 120 seconds.  If approval is not complete, let it expire
and create a new plan rather than extending or editing it.

```powershell
$Plan = Get-Content -Raw $CanaryPlan | ConvertFrom-Json
$env:HYDRA_LIVE_CANARY_CONFIRM_SHA256 = $Plan.plan_sha256
$env:HYDRA_LIVE_TRADING_ENABLED = "true"
$env:HYDRA_LIVE_CANARY_ENABLED = "true"
python -m live_client.canary submit --plan $CanaryPlan
$env:HYDRA_LIVE_CANARY_ENABLED = "false"
$env:HYDRA_LIVE_TRADING_ENABLED = "false"
```

Close both switches even if the command errors.  Once the adjacent
`.submit-lock.json` exists, never run `submit` again for that plan.  An error after
the durable intent may mean the broker received the order; use `status` or inspect
MiniQMT by the exact `HC|...` remark.

## Phase 3 — observe terminal state

```powershell
python -m live_client.canary status --plan $CanaryPlan
```

Record the raw QMT order status, message, order ID, system ID, submitted quantity,
filled quantity and VWAP.  `ACTIVE_OR_UNKNOWN` is not a terminal result and must
not be interpreted as cancelled.

If the exact order remains active and the approved response is to cancel it, keep
the trading switch off and issue a separately confirmed cancellation:

```powershell
$env:HYDRA_LIVE_TRADING_ENABLED = "false"
$env:HYDRA_LIVE_CANARY_CANCEL_CONFIRM_SHA256 = $Plan.plan_sha256
python -m live_client.canary cancel --plan $CanaryPlan
python -m live_client.canary status --plan $CanaryPlan
```

`cancel_sent=true` means MiniQMT accepted the cancellation instruction; it is not
proof of cancellation.  Repeat `status` until QMT reports an explicit terminal
status.  The script cancels only the unique order whose account, symbol, quantity,
price, strategy name and remark all match the immutable plan and which QMT lists
as cancelable.

## Phase 4 — evidence and ledger treatment

Preserve all three adjacent artifacts:

- the immutable plan JSON;
- `.submit-lock.json`;
- `.events.jsonl`, whose records form a SHA-256 chain.

Also export the corresponding broker order/trade evidence from MiniQMT.  Compare
the QMT account cash and positions before and after the canary.

If filled, do not post the canary as a formal Hydra server trade.  Instead, rerun
the authorized live account initialization/reconciliation so the formal Hydra
baseline starts from the actual cash and the additional 100-unit position.  Do
not stage a formal target until this reconciliation is clean.

The official XtQuant contract used here states that `order_stock` returns a
positive broker order ID on successful submission and `-1` on failure, while
`cancel_order_stock` returning `0` means only that the cancellation instruction
was sent successfully.  Broker order status remains the terminal authority:
<https://dict.thinktrader.net/nativeApi/xttrader.html>.

## GO record

Before Phase 2, record all of the following outside the repository:

- operator and approver;
- execution date/time and real account fingerprint suffix;
- selected symbol and plan SHA-256;
- approved CNY 2,000 hard ceiling and expected 100-unit notional;
- whether an unfilled order will be cancelled and after how long;
- who will perform the post-fill account initialization/reconciliation.
