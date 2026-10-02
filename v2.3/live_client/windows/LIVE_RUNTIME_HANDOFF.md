# Hydra live Windows runtime

This directory contains the non-secret, versioned Windows-side runtime used for
Hydra live. The installer records the exact approved source commit in each
release manifest and in `active-release.txt`.

## Deployed layout

- The versioned client is installed under `C:\hydra-live\releases\<commit>\live_client`.
- `Run-HydraLive.ps1` is installed as `C:\hydra-live\bin\Run-HydraLive.ps1`.
- The operations scripts are installed under `C:\hydra-live\scripts`.
- `hydra-live.env`, account IDs, webhook URL, API keys, state DB, logs, evidence and data packages are private runtime material and intentionally excluded from Git.

## Scheduled workdays

| Time | Task | Client action |
|---|---|---|
| 14:55 | `Hydra-Live-CancelOpen-1455` | Request cancellation only for active orders belonging to the frozen Hydra batch. A request acknowledgement is not treated as a terminal cancellation. |
| 15:30 | `Hydra-Live-MarketBackup-1530` | Store an isolated live-QMT market-data backup. |
| 16:05 | `Hydra-Live-SettleClose-1605` | Read the broker's post-16:00 terminal report, push trades, reconcile and close the attempt; on process failure, retry once at 16:10. |
| 16:20 | `Hydra-Live-Retry-1620` | If the close receipt says `RESIDUAL`, ask the Hydra relay to stage the next attempt. |
| 18:00 | `Hydra-Live-QueryPreflight-1800` | Pull/freeze the next trading day's batch, persist its online preflight receipt, then idempotently create `Hydra-Live-Submit-YYYYMMDD-0910`. |
| T+1 09:10 | `Hydra-Live-Submit-YYYYMMDD-0910` | One-time offline submit using the frozen batch and PASS receipt; no server call and no automatic task retry. |

`Invoke-HydraLiveSubmit.ps1` performs only offline submit and relies on the prior
evening's frozen batch and PASS receipt. `Register-HydraLiveSubmitTask.ps1`
refuses to overwrite a same-name task whose action or trigger differs. The
MiniQMT connection retry in `gateway.py` applies only before account or broker
order operations; every failed candidate is stopped, and it never retries an
order API call.

`cancel-open` is intentionally scheduled before the exchange's 14:57 no-cancel
window. It first proves account, symbol, quantity, price, direction, strategy
name and deterministic remark for the complete local Hydra submission set. An
identity mismatch blocks every cancellation. Per-order request failures are
reported without stopping requests for the other already-validated Hydra
orders, and every run writes a SHA-256 evidence receipt. The 16:05 task still
requires explicit terminal QMT states; `ORDER_PARTSUCC_CANCEL` remains active.

Before enabling residual retry, configure all three values for the same approved
cycle: take `HYDRA_LIVE_RETRY_EXECUTION_RAW_SHA256` from the canonical raw
manifest, and take `HYDRA_LIVE_RETRY_TARGET_ID` plus
`HYDRA_LIVE_RETRY_REBALANCE_ID` from the server stage response or frozen batch.
