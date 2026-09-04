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
| 15:10 | `Hydra-Live-SettleClose-1510` | Push terminal QMT order state, reconcile the account and close the Hydra attempt; on process failure, retry once after five minutes. |
| 15:30 | `Hydra-Live-MarketBackup-1530` | Store an isolated live-QMT market-data backup. |
| 16:00 | `Hydra-Live-Retry-1600` | If the close receipt says `RESIDUAL`, ask the Hydra relay to stage the next attempt. |
| 18:00 | `Hydra-Live-QueryPreflight-1800` | Pull/freeze the next trading day's batch, persist its online preflight receipt, then idempotently create `Hydra-Live-Submit-YYYYMMDD-0910`. |
| T+1 09:10 | `Hydra-Live-Submit-YYYYMMDD-0910` | One-time offline submit using the frozen batch and PASS receipt; no server call and no automatic task retry. |

`Invoke-HydraLiveSubmit.ps1` performs only offline submit and relies on the prior
evening's frozen batch and PASS receipt. `Register-HydraLiveSubmitTask.ps1`
refuses to overwrite a same-name task whose action or trigger differs. The
MiniQMT connection retry in `gateway.py` applies only before account or broker
order operations; every failed candidate is stopped, and it never retries an
order API call.

Before enabling residual retry, configure all three values for the same approved
cycle: take `HYDRA_LIVE_RETRY_EXECUTION_RAW_SHA256` from the canonical raw
manifest, and take `HYDRA_LIVE_RETRY_TARGET_ID` plus
`HYDRA_LIVE_RETRY_REBALANCE_ID` from the server stage response or frozen batch.
