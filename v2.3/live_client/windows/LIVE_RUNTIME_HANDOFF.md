# Hydra live Windows runtime

This directory contains the non-secret Windows-side runtime used for Hydra live.
It is based on the packaged live-client source commit `be32a27827dd152c0da0c239d9bb5b58914ae3f1`.

## Deployed layout

- The versioned client is installed under `C:\hydra-live\releases\<commit>\live_client`.
- `Run-HydraLive.ps1` is installed as `C:\hydra-live\bin\Run-HydraLive.ps1`.
- The operations scripts are installed under `C:\hydra-live\scripts`.
- `hydra-live.env`, account IDs, webhook URL, API keys, state DB, logs, evidence and data packages are private runtime material and intentionally excluded from Git.

## Scheduled workdays

| Time | Task | Client action |
|---|---|---|
| 15:10 | `Hydra-Live-Settle-1510` | Read QMT cumulative order state and push settlement result. |
| 15:30 | `Hydra-Live-MarketBackup-1530` | Store an isolated live-QMT market-data backup. |
| 16:00 | `Hydra-Live-Trigger-1600` | Invoke the existing server trigger; residual-order decisions remain server-side. |
| 18:00 | `Hydra-Live-Query-1800` | Pull and freeze the next trading day's server batch locally. |

`Invoke-HydraLiveSubmit.ps1` is deliberately manual/one-time: it performs a live preflight and submits only when the result is `READY_FOR_OFFLINE_SUBMIT`. The MiniQMT connection retry in `gateway.py` applies only before account or broker-order operations; it never retries an order API call.
