# Hydra independent live client

This package is intentionally separate from `v2.3/client`. It never imports the
paper client configuration and must use its own QMT userdata, session id, SQLite,
logs, API key and Windows Task Scheduler names.

## Safety defaults

- `HYDRA_LIVE_EXECUTION_DOMAIN` must be exactly `live`.
- The configured QMT account must match a private SHA-256 fingerprint and must not
  appear in the private paper-account denylist.
- When paper and live clients share a Windows device (the safe default), real
  mode requires non-empty paper account/session/path/task-prefix denylists and
  rejects overlap. For separate Windows devices, set
  `HYDRA_LIVE_PAPER_CLIENT_COLOCATED=false`; server-side paper/live domain and
  account-alias isolation remain mandatory.
- Real mode requires an explicit transport decision. HTTPS remains the generic
  default; the current owner-approved deployment may use
  `http://120.26.138.82:8000` only when its private Windows configuration sets
  `HYDRA_LIVE_ALLOW_INSECURE_HTTP=true`. This is an acknowledgement switch, not
  encryption, and it does not relax Bearer authentication or any trading gate.
- `HYDRA_LIVE_TRADING_ENABLED=false` blocks submit, including mock submit.
- `HYDRA_LIVE_LEDGER_MODE=attributed` makes QMT the physical container only.
  Hydra capacity comes from its server-side `virtual_cash` and attributed
  positions; unallocated QMT cash and other strategies' positions are unusable.
- `HYDRA_LIVE_RISK_MODE=disabled` independently blocks every live batch. `auto`
  computes limits from Hydra's attributed equity, available cash and sellable
  holdings; its snapshot is written once per batch before the first submission.
- Every server batch is independently re-hashed and frozen by `query`. The
  required online `preflight` compares QMT with the server ledger and persists a
  hashed PASS receipt for that exact batch.
- `submit` never constructs an HTTP client. It re-hashes only the frozen local
  batch, requires its PASS receipt, verifies the live QMT account and current
  capacity, then submits SELL before BUY even if the server is unavailable.
- Each order intent is committed locally before the QMT call. A deterministic QMT
  remark recovers a broker-accepted order after a client crash; an ambiguous call
  with no observable broker order is never retried automatically.
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
python -m live_client.cli doctor
python -m live_client.cli ledger
python -m live_client.cli query --date YYYYMMDD
python -m live_client.cli preflight --date YYYYMMDD
python -m live_client.cli submit --date YYYYMMDD
python -m live_client.cli settle-close --date YYYYMMDD
python -m live_client.cli retry --date YYYYMMDD --next-date YYYYMMDD
python -m live_client.cli cash-flow --date YYYYMMDD --type DIVIDEND `
  --amount 123.45 --source <verified-source> --source-event-id <stable-id> `
  --evidence-sha256 <sha256>
python -m live_client.cli cash-flow --date YYYYMMDD `
  --type CAPITAL_DEALLOCATION --amount -123.45 --source owner-allocation `
  --source-event-id <stable-id> --evidence-sha256 <sha256> `
  --transition-to-attributed
python -m live_client.cli reconcile-close --attempt-id <attempt_id> --evidence-sha256 <sha256>
```

`doctor` validates the private configuration and performs only the compatible
SQLite schema migration. It reports `server_contacted=false` and
`qmt_contacted=false`; it is safe to use during a client code deployment.

## Versioned Windows deployment

The supported Windows entrypoint is versioned and leaves the private env,
SQLite and logs outside the release directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\live_client\windows\Install-HydraLiveClient.ps1 `
  -SourceRoot C:\src\MaolaoyeServer\v2.3 `
  -InstallRoot C:\hydra-live `
  -PythonExe C:\path\to\python.exe
```

The installer refuses a dirty `live_client` source, copies it to
`C:\hydra-live\releases\<full-git-sha>`, runs Python syntax validation and the
synthetic offline-submit acceptance, then atomically switches
`config\active-release.txt`. It never overwrites `config\hydra-live.env`, the
state database or logs. A local-only `doctor` is the final activation check; if
that fails, the active pointer is rolled back. Existing Task Scheduler entries
are deliberately not modified.

Use the stable runner in Task Scheduler and always supply the already-approved
exchange trade date explicitly:

```powershell
C:\hydra-live\bin\Run-HydraLive.ps1 -Command query -Date YYYYMMDD
C:\hydra-live\bin\Run-HydraLive.ps1 -Command preflight -Date YYYYMMDD
C:\hydra-live\bin\Run-HydraLive.ps1 -Command submit -Date YYYYMMDD
C:\hydra-live\bin\Run-HydraLive.ps1 -Command settle-close -Date YYYYMMDD
C:\hydra-live\bin\Run-HydraLive.ps1 -Command retry -Date YYYYMMDD -NextDate YYYYMMDD
```

Do not derive `YYYYMMDD` by adding one calendar day: month boundaries, weekends
and exchange holidays must come from the frozen trading calendar. See
[`WINDOWS_DEPLOYMENT_RUNBOOK.md`](WINDOWS_DEPLOYMENT_RUNBOOK.md) for upgrade,
acceptance, task cutover and rollback.

The portable no-network acceptance can also be run directly:

```powershell
python -m live_client.offline_acceptance
```

It proves three properties with synthetic orders and `mock_qmt`: a dead server
cannot block local submit, repeating submit creates no second broker call, and
crash/ambiguous-response recovery never blindly replays an order.

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
  --output C:\private\hydra\HYDRA_QMT_SNAPSHOT_YYYYMMDD
```

By default it queries QMT dividend factors for the nine executable ETFs and
freezes them into the corporate-actions bundle. `--corporate-actions <parquet>`
is an optional, separately audited import override.

It refuses to overwrite a pre-existing output directory, ZIP, or receipt. Run
it only after the QMT daily bars are complete; it is a market-data operation,
not an account query or an order operation.

For mock runs, append `--mock-state C:\private\hydra-live\mock-state.json` to
commands that access QMT. Query still calls the domain-scoped server API.

Supported Windows task names:

- `Hydra-Live-QueryPreflight-1800` — T evening query followed by online preflight.
- `HydraLive-OrderSubmit` — T+1 09:10, local frozen batch and MiniQMT only.
- `Hydra-Live-SettleClose-1510` — terminal QMT status, trade result, reconciliation and attempt close.
- `Hydra-Live-MarketBackup-1530` — isolated live-QMT market backup with explicit receipt.
- `Hydra-Live-Retry-1600` — Hydra retry only after a server-confirmed residual.
- `HydraLive-CashFlowJournal` — daily after verified dividend/fund-flow evidence.
- `HydraLive-DataFreeze` — month-end after QMT daily data is complete.

These tasks connect directly to MiniQMT through `xtquant`; there is no large-QMT
transition adapter in this release.

Do not create these tasks until the mock and dedicated-paper acceptance gates in
the server runbook have passed.

## One-lot real MiniQMT canary

`python -m live_client.canary` is a deliberately separate broker-path test.  It
does not fetch, slice or settle a Hydra server batch because doing so would leave
a false partial batch in the production ledger.  It is restricted to one BUY of
100 units of `510300.SH` or `159915.SZ`, a CNY 2,000 hard notional ceiling, a
fresh limit-price plan, and independent plan-hash and kill-switch confirmation.

The canary can create a real-money order.  Follow
[`MINIQMT_LIVE_CANARY_RUNBOOK.md`](MINIQMT_LIVE_CANARY_RUNBOOK.md) exactly.  Any
fill changes the real account and must be captured by a new account
initialization/reconciliation before the formal Hydra target is staged.
