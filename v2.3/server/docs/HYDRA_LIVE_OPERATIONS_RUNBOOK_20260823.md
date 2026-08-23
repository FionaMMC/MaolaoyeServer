# Hydra → QMT operational runbook

## Current release decision

**NO-GO for real orders.** The repository-side foundation is implemented, but
production remains unchanged and every live switch defaults to off. Real enablement
still requires private credentials/account paths, approved numeric limits, an
approved publisher commit, production data snapshots, Windows/MiniQMT deployment,
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

## Capital preflight

Run the current month, not only a historical threshold:

```bash
python -m scripts.hydra_capital_preflight \
  --target Hydra_latest.parquet --execution-raw data.parquet \
  --as-of YYYYMMDD --capital 100000 --capital 700000 --capital 1500000
```

Historical replay found approximately ¥100k as a technical floor, ¥700k as the
preferred pilot scale (worst historical single-ETF error near 2 percentage points),
and ¥1.5m for roughly 1-point fidelity. Lot rounding is saw-toothed, so the current
month report is an approval artifact, not an informational dashboard.

## Required order-day sequence

1. T close: freeze HFQ/raw/actions/calendar and hashes; produce and validate target.
2. T evening: stage T+1 attempt; run capital/risk preflight; live client `query`.
3. T+1 09:15: live client `submit` re-fetches the server batch, reads QMT total and
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

- [ ] Business supplies and approves all non-zero server and client risk limits.
- [ ] Private live token/client id/account alias/fingerprint and isolated paths installed.
- [ ] New Hydra publisher commit and dual-data producer commit reviewed and allowlisted.
- [ ] 81-month HFQ-vs-raw/corporate-action differential audit signed off.
- [ ] Two identical mock basket replays produce identical hashes and no real QMT call.
- [ ] Dedicated paper account completes one full month-end/month-start cycle.
- [ ] Recovery drill covers partial fill, residual retry, emergency stop and audit export.
- [ ] Business gives written approval for a capped live pilot.
