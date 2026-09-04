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
| 15:10 | `Hydra-Live-SettleClose-1510` | Push terminal QMT order state, reconcile the account and close the Hydra attempt. |
| 15:30 | `Hydra-Live-MarketBackup-1530` | Store an isolated live-QMT market-data backup. |
| 16:00 | `Hydra-Live-Retry-1600` | If the close receipt says `RESIDUAL`, ask the Hydra relay to stage the next attempt. |
| 18:00 | `Hydra-Live-QueryPreflight-1800` | Pull/freeze the next trading day's batch and persist its online preflight receipt. |

`Invoke-HydraLiveSubmit.ps1` is deliberately manual/one-time: it performs only offline submit and relies on the prior evening's frozen batch and PASS receipt. The MiniQMT connection retry in `gateway.py` applies only before account or broker-order operations; it never retries an order API call.
