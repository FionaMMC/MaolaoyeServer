# Hydra → QMT operational runbook

## Current release decision

**NO-GO for real orders.** The repository-side foundation is implemented, but
production remains unchanged and every live switch defaults to off. Real enablement
still requires private credentials/account paths, approved risk mode, an approved
publisher commit, the formal 2026-08-31 snapshot, Windows/MiniQMT deployment,
mock and dedicated-paper evidence, and written business acceptance.

No real QMT account id, API key, userdata path or password belongs in Git. Server
and client use a non-sensitive alias such as `hydra-live`; the real account is
checked through a private SHA-256 fingerprint.

## Delivered controls

- `execution_domain` now follows `InstanceState`, `RawSignal`, `Order`, `Trade`,
  performance, reconciliation, target/rebalance/attempt state, cash flows and
  execution-quality observations. Historical rows migrate to `paper`.
- Paper and live use different Bearer tokens. A live token is also bound to a
  `client_id` and account-alias allowlist.
- Live order generation and order delivery are separate server switches. Account
  initialization and cash-flow ingestion have their own switches.
- Hydra data is stored in four immutable content-addressed streams:
  `hydra_model_hfq`, `hydra_execution_raw`, `hydra_corporate_actions`, and
  `hydra_trading_calendar`; the relay proves that execution is the next trading day.
- The relay validates target weights, ETF allowlist, publisher SHA, dual-data dates,
  hashes, suspension, 100-unit buy lots, ¥0.001 tick rounding, cash feasibility and
  server risk limits.
- Manifest audit fields and the executable/research-only universe survive staging;
  research bridge `511010.SH` is accepted only in HFQ and rejected from raw prices,
  targets and orders.
- Live risk has explicit `disabled/static/auto` modes. The default is `disabled`.
  `auto` derives notional limits from reconciled QMT NAV, cash and sellable holdings,
  retains the 50bp protection, and persists its computed snapshot.
- Monthly idempotency is split into immutable target, rebalance and daily attempt.
  A retry cannot be created until the previous attempt is terminal and post-trade
  reconciliation records an actual residual.
- Dividends/deposits/withdrawals use an idempotent external-cash-flow journal.
- The independent live client re-fetches and re-hashes the server batch immediately
  before submission. It never replaces a changed local batch or continues after a
  network failure.
- Every fill can record decision price, arrival price/time, submitted price/time,
  QMT order id, VWAP, IOPV, decision gap, execution shortfall, premium and fees.

## Server deployment rehearsal

Run these first on a copy of the production SQLite database and data directory:

```bash
cd /opt/qmt-server/v2.3/server
HYDRA_REHEARSAL_DB="$(mktemp /tmp/hydra-migration.XXXXXX)"
cp pipeline-server.db "$HYDRA_REHEARSAL_DB"
venv/bin/python -m scripts.migrate_db \
  --db-url "sqlite:///$HYDRA_REHEARSAL_DB" --skip-stale-cleanup
venv/bin/python -m scripts.migrate_db \
  --db-url "sqlite:///$HYDRA_REHEARSAL_DB" --skip-stale-cleanup
venv/bin/pytest tests -q
```

`migrate_db` is idempotent. It creates the new tables, adds domain/lineage columns,
and labels all existing rows `paper`. Verify row counts and retain the backup. Do not
delete the backup or any target/order/trade evidence during rollback.

Only after the rehearsal evidence is accepted, make a timestamped backup and run the
same migration against the configured database:

```bash
cp pipeline-server.db "pipeline-server.db.pre-hydra-$(date +%Y%m%d%H%M%S)"
venv/bin/python -m scripts.migrate_db
```

The first code-only deployment keeps every `QMT_LIVE_*_ENABLED=false`. Confirm the
existing paper client still retrieves only paper orders and completes one normal
settlement/reconciliation cycle before any live credential is installed.

## Data freeze and target staging

On the isolated Windows host, freeze QMT HFQ and raw prices plus an independently
verified corporate-action parquet:

```powershell
python -m live_client.data_snapshot --as-of YYYYMMDD `
  --producer-commit <full-sha> `
  --corporate-actions C:\private\hydra\corporate_actions.parquet `
  --output C:\private\hydra\snapshots\YYYYMMDD
```

Transfer each stream to the server through the approved encrypted channel and stage
it without overwriting an existing content address:

```bash
python -m scripts.stage_hydra_data --parquet data.parquet \
  --manifest manifest.json --root /var/lib/qmt-pipeline
```

Convert the approved Hydra producer target/sidecar and the frozen data snapshot into
the relay request:

```bash
python -m scripts.build_hydra_target_request \
  --target Hydra_latest.parquet --sidecar Hydra_latest.json \
  --data-snapshot snapshot.json --execution-date YYYYMMDD \
  --execution-domain paper --account-alias hydra-paper \
  --instance-id paper_hydra_v481_rb --output target-request.json
```

First run it in paper/mock. Only an approved operator may later change the request to
`live` and POST it to `/hydra/targets/stage` with the live token.

The strategy cash-buffer default is `0`. For live sizing, the relay independently
reserves the worst permitted buy-price offset plus 0.1% execution-cost headroom;
this is an execution solvency control, not a change to Hydra target weights.

## Capital preflight

Run the current month, not only a historical threshold:

```bash
python -m scripts.hydra_capital_preflight \
  --target Hydra_latest.parquet --execution-raw data.parquet \
  --as-of YYYYMMDD --capital 100000 --capital 200000 --capital 700000
```

Historical replay found approximately ¥100k as a technical floor and ¥700k as the
higher-fidelity scale (worst historical single-ETF error near 2 percentage points).
The resolved first pilot is **¥200,000**: usable for grey release but explicitly not
high-fidelity (historical worst single-ETF deviation about 6.2–6.4pp). Lot rounding
is saw-toothed, so the formal 2026-08-31 report at ¥200k is an approval artifact.

## Required order-day sequence

1. T close: freeze HFQ/raw/actions/calendar and hashes; produce and validate target.
2. T evening: stage T+1 attempt; run capital/risk preflight; live client `query`.
3. T+1 09:10: live client `submit` re-fetches the server batch, reads QMT total and
   sellable positions separately, requires a clean server reconciliation, checks
   cash and client limits, then submits SELL before BUY.
4. T+1 close: `settle` sends cumulative fills and execution-quality evidence.
   It fails closed if QMT still reports an active or unknown order status.
5. Ingest approved dividends/deposits/withdrawals with a stable source event id and
   evidence hash, for example:

   ```powershell
   python -m live_client.cli cash-flow --date YYYYMMDD --type DIVIDEND `
     --amount 123.45 --source <verified-source> --source-event-id <stable-id> `
     --evidence-sha256 <sha256>
   ```

6. `reconcile-close` compares QMT with the virtual ledger. It marks the attempt
   `COMPLETE` only when there is no residual; otherwise it records `RESIDUAL`.
7. A next-day retry is staged through `/hydra/rebalances/retry`; it references the
   same target/rebalance and cannot run while an earlier order is unresolved.

Windows Task Scheduler uses the agreed MiniQMT-only sequence: 15:10 QMT status
collection, 15:30 terminal settlement/reconciliation, 16:00 eligible residual
staging, 18:00 audit/export, and T+1 09:10 re-fetch/hash/account checks plus submit.
No large-QMT adapter or transition path is required.

## Slippage, premium and month-end evidence

```bash
python -m scripts.hydra_execution_quality_report \
  --db-url sqlite:///pipeline-server.db --target-id <target_id> \
  --execution-domain live

python -m scripts.build_hydra_audit_bundle \
  --db-url sqlite:///pipeline-server.db --target-id <target_id> \
  --instance-id <instance_id> --output-dir /private/audit/<target_id>
```

Decision gap, broker execution shortfall and ETF premium are reported separately.
The bundle refuses to overwrite a non-empty directory and hashes target, rebalance,
attempts, orders, trades, execution quality, cash flows and month-end state.

## Emergency stop and rollback

1. Set the client `HYDRA_LIVE_TRADING_ENABLED=false`.
2. Set server `QMT_LIVE_ORDER_DELIVERY_ENABLED=false`, then
   `QMT_LIVE_ORDER_GENERATION_ENABLED=false` and restart the service.
3. Query QMT for every submitted order. Cancel only explicitly confirmed unresolved
   QMT order ids using the broker workflow; do not infer cancellation from local state.
4. Push final cumulative status, ingest outstanding external cash flows, and run
   reconciliation. Escalate every unmatched id or bookkeeping divergence.
5. Preserve SQLite, local live-client DB, targets, data manifests, QMT evidence,
   logs and audit bundles. Never “recover” by deleting rows.
6. Roll code back only after database compatibility is verified. New nullable tables
   and columns may remain; do not reverse-migrate production evidence.

## GO gate requiring external action

- [x] Business selected dynamic `auto` technical risk; code defaults to disabled and
      no static business amount cap is interpreted as unlimited.
- [ ] Private live token/client id/account alias/fingerprint and isolated paths installed.
- [ ] New Hydra publisher commit and dual-data producer commit reviewed and allowlisted.
- [ ] 81-month HFQ-vs-raw/corporate-action differential audit signed off.
- [ ] Two identical mock basket replays produce identical hashes and no real QMT call.
- [ ] Dedicated paper account completes one full month-end/month-start cycle.
- [ ] Recovery drill covers partial fill, residual retry, emergency stop and audit export.
- [ ] Business gives written approval for a capped live pilot.

## 2026-08-25 delivery intake

- `HYDRA_QMT_SNAPSHOT_20260821-r1.zip` and its `(1)` copy are byte-identical
  (`c95211c0becf2df73af0c9c8395e3bcd392a64691858dfe8814cbec3db624e0e`).
  The archive is path-safe and all four immutable stream hashes, row counts,
  schemas, dates and symbol coverage validate. It is accepted for smoke/research.
- It is **not** a formal month-end package: `as_of_date=20260821`, so it cannot
  publish the 2026-09 target. Formal freeze remains 2026-08-31 and execution date
  2026-09-01, subject to the frozen calendar.
- The referenced `4941029` object was absent locally and remotely, so it was not
  represented as an existing commit. Its documented month-end guard was independently
  rebuilt, tested and pushed as `8ebfd21a159c74b73397ffb3847878a597d055df`;
  both research candidate branches now point to that full SHA.
- `research_delivery/audit_20260821` is now present remotely with every artifact
  actually received plus an immutable receipt manifest. The manifest lists nine
  referenced-but-not-received files, so this remains partial smoke evidence and
  cannot authorize formal publisher allowlisting.
