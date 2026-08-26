# Hydra independent live client

This package is intentionally separate from `v2.3/client`. It never imports the
paper client configuration and must use its own QMT userdata, session id, SQLite,
logs, API key and Windows Task Scheduler names.

## Safety defaults

- `HYDRA_LIVE_EXECUTION_DOMAIN` must be exactly `live`.
- The configured QMT account must match a private SHA-256 fingerprint and must not
  appear in the private paper-account denylist.
- Real mode requires non-empty paper account/session/path/task-prefix denylists;
  the live values must not overlap any of them.
- Real mode requires HTTPS unless a separately audited private network explicitly
  opts into insecure HTTP.
- `HYDRA_LIVE_TRADING_ENABLED=false` blocks submit, including mock submit.
- `HYDRA_LIVE_RISK_MODE=disabled` independently blocks every live batch. `auto`
  computes limits from the current QMT total asset, available cash and sellable
  holdings; its snapshot is written once per batch before the first submission.
- Every server batch is independently re-hashed. A network failure, mixed domain,
  wrong account alias, changed batch, missing lineage field or risk-limit breach
  aborts submission.
- Immediately before submission, the client reads QMT total positions/cash and
  requires a clean server reconciliation. Sell capacity is checked separately
  against QMT `can_use_volume` rather than total holdings.
- Settlement refuses to infer `CANCELLED` from an active, missing or unknown QMT
  order status.
- The code imports `xtquant` only after `HYDRA_LIVE_MODE=live` connects. Tests and
  `mock_qmt` cannot enter the real QMT adapter.

Copy `.env.example` into a private secret-management mechanism. The package does
not auto-load `.env`; Windows tasks must inject environment variables explicitly.

## Command sequence

From the `v2.3` directory with `PYTHONPATH` pointing to that directory:

```powershell
python -m live_client.cli initialize-account --evidence-sha256 <sha256>
python -m live_client.cli query --date YYYYMMDD
python -m live_client.cli submit --date YYYYMMDD
python -m live_client.cli settle --date YYYYMMDD
python -m live_client.cli cash-flow --date YYYYMMDD --type DIVIDEND `
  --amount 123.45 --source <verified-source> --source-event-id <stable-id> `
  --evidence-sha256 <sha256>
python -m live_client.cli reconcile-close --attempt-id <attempt_id> --evidence-sha256 <sha256>
```

## Research data freeze (read-only)

The data freezer is deliberately separate from the trading configuration: it
accepts only a QMT `userdata` path and never reads an account id, API key,
session id or live-client `.env`.  It writes four date-consistent bundles plus
one write-once ZIP and an external SHA-256 receipt.  The model HFQ bundle has
the nine executable ETFs and research-only `511010.SH`; the raw execution
bundle has only the nine executable ETFs.

```powershell
python -m live_client.data_snapshot --as-of YYYYMMDD `
  --producer-commit <full-40-char-hydra-sha> `
  --userdata-dir C:\private\qmt\userdata `
  --corporate-actions C:\private\hydra\corporate_actions.parquet `
  --output C:\private\hydra\HYDRA_QMT_SNAPSHOT_YYYYMMDD
```

It refuses to overwrite a pre-existing output directory, ZIP, or receipt. Run
it only after the QMT daily bars are complete; it is a market-data operation,
not an account query or an order operation.

For mock runs, append `--mock-state C:\private\hydra-live\mock-state.json` to
commands that access QMT. Query still calls the domain-scoped server API.

Suggested distinct Windows task names:

- `HydraLive-TargetQuery` — month-end evening after the server stages T+1 orders.
- `HydraLive-OrderSubmit` — T+1 09:10.
- `HydraLive-QMTStatus` — 15:10.
- `HydraLive-TradeSettle` — 15:30 after QMT reports terminal status.
- `HydraLive-ResidualStage` — 16:00 only after clean reconciliation.
- `HydraLive-AuditExport` — 18:00.
- `HydraLive-CashFlowJournal` — daily after verified dividend/fund-flow evidence.
- `HydraLive-ReconcileClose` — after settlement and external cash-flow ingestion.
- `HydraLive-DataFreeze` — month-end after QMT daily data is complete.

These tasks connect directly to MiniQMT through `xtquant`; there is no large-QMT
transition adapter in this release.

Do not create these tasks until the mock and dedicated-paper acceptance gates in
the server runbook have passed.
