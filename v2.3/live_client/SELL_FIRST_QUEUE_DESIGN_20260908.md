# Sell-first execution queue — local first slice, not deployed

## Implemented behavior

The preflight plan may be feasible because approved sales can fund approved
buys. This projection is **not cash already available for trading**.

Each offline `submit` invocation now makes one queue pass:

1. Hold a same-account process lock under the configured QMT userdata path.
   Cooperating strategies using different state databases but the same userdata
   and account use the same lock. No server request or database transaction is
   held while the broker is called.
2. Submit already-approved SELL orders first, preserving existing order IDs,
   limits, broker remarks and unknown-call recovery behavior.
3. Before each new BUY, read identity-matched cumulative SELL fills and fresh
   QMT available cash. An accepted SELL with no fill contributes zero cash.
4. Buying capacity is the smaller of physical available cash and the strategy's
   frozen cash plus its confirmed sell proceeds minus already-submitted buy
   reservations. Another strategy's or the reserve account's cash is not credit.
   The local pass also subtracts its own new buy reservations so an asset-query
   lag cannot spend the same physical cash twice.
5. Insufficient cash leaves the order as `DEFERRED_CASH`, which proves that the
   broker submission call was not made. The result is `WAITING_FOR_CASH`, not
   success, broker rejection or unknown submission. The Windows notice says so.
6. An explicit subsequent pass can continue only the still-unsubmitted orders.
   It does not retry already-submitted, locally closed or unknown broker calls.
   The cumulative sell evidence is persisted and cannot regress silently after
   a restart.
7. Explicit `settle-close` ends deferred orders locally as `NOT_SUBMITTED`, with
   `not_submitted_reason=INSUFFICIENT_CASH`, zero fills and no QMT order ID.
   This is a client execution fact, not a claim that the broker rejected an order.
   Merely calling `settle` does not end the cash-wait queue.

QMT partial-cancel status 53 maps to `CANCELLED` with the actual cumulative filled
quantity; active partial status 55 remains active. This fixes the prior ambiguous
terminal `PARTIAL` representation. A confirmed partial sell fill may fund a buy
without pretending the remainder of the sell is terminal.

`settle` now ingests each independently validated observation: active partial
fills are sent as `PARTIAL`, zero-fill active orders remain pending, and a missing
or mismatched broker order does not discard another order's confirmed fill.
`settle-close` returns `WAITING_FOR_BROKER` after sending available facts when any
order remains unresolved; the Windows wrapper reports pending evidence, not
successful final settlement or a missing-receipt runtime failure. This does not
create an automatic polling schedule, so pending evidence still needs the next
approved observation pass.

## Explicit execution-window Close

An additive CLI entry point now supports the new business Close definition:

```text
python -m live_client.cli close-window --date YYYYMMDD --execution-deadline-at YYYY-MM-DDTHH:MM:SS+08:00
```

The installed Windows runner also accepts `-Command close-window -Date YYYYMMDD
-ExecutionDeadlineAt <zoned-deadline>`. No new scheduled task is registered here.

Supply the broker/product execution deadline approved for that batch; the CLI
requires a timezone and an elapsed deadline. This command does not connect to
QMT, query funds, fabricate a cash snapshot or wait for broker terminal states.
It persists a local window-close intent before contacting the server. Even if
that HTTP call fails, the same local batch cannot submit new orders after this
explicit local close. Retrying the command sends the same close intent, not an
order. The server response is checked against the frozen attempt/target/rebalance
and saved as a separate `close-window` receipt, never mistaken for a final
reconciled residual receipt. A changed new-order risk policy cannot block this
close; batch/account integrity checks still apply.

An old broker order may still exist after the execution window is closed. Its
funds/positions remain obligated until its actual outcome is reconciled. Deferred
orders never sent to the broker become local `NOT_SUBMITTED`; the subsequent
settlement report conveys that fact to the server. This command does not grant
permission to submit replacement orders or dispose of the broker order.

## Deployment contracts and remaining work

- Server support for `NOT_SUBMITTED` must precede this client version. Server
  close and residual logic must recognize this zero-fill local terminal fact.
  Do not deploy the client alone to an older server that rejects the status.
- The server must also support `close_mode=execution_deadline` with omitted cash
  and positions before using `close-window`. Existing scheduled `settle-close`
  tasks are unchanged; the new CLI is not automatically scheduled by this slice.
- This slice is a durable, resumable queue **pass**, not a background daemon. No
  production task has been changed and no automatic polling schedule is added.
  A 09:10 sale may not fill until the auction. The next approved iteration must
  implement the broker/product execution window and resume only known-unsent
  orders in that window; it must never retry an ambiguous `order_stock` call.
- The same frozen order is neither resized nor repriced. If a partial sale funds
  less than its full approved BUY, that BUY waits. Order splitting requires an
  explicit parent/child allocation protocol, not locally invented new orders.
- Each buy and sell reserves the larger of a rate and a minimum commission:
  `max(notional × HYDRA_LIVE_EXECUTION_COST_RESERVE_BPS / 10000,
  HYDRA_LIVE_EXECUTION_MIN_COMMISSION)`. Defaults are 10 bps and CNY 5, including
  small orders whose percentage-only fee buffer would be insufficient. The
  preflight receipt and initial queue evidence freeze this policy. It is still
  a configurable estimate, not an exact guarantee of the broker's fee tariff.
  Confirm the actual broker/product tariff before rollout and reconcile actual
  fees. Currency calculations round conservatively to cents.
- These locks coordinate only updated clients on one QMT userdata path. They do
  not prevent manual orders, legacy clients, a second host or another userdata
  path from using the same physical account. Full multi-strategy deployment
  still needs the shared account executor and server-owned resource grants.
- Frozen owner cash is not a live server grant. Capital cannot be reallocated
  away from an outstanding offline batch merely because the server's clock or
  configuration changes. Use the server's resource handback rules at migration.
- Confirmed broker quantities and prices are the evidence used here. Missing,
  mismatched or regressing evidence does not fabricate proceeds. The current
  broker query implementation is bounded; a failed query stops this pass while
  retaining the last safe local states.
- The local account lock releases automatically when the process exits. Its
  existing file does not mean that a stale worker still owns it; do not delete
  state databases or lock files as a recovery method.

No live broker, server, private configuration, Windows task or production ledger
was changed during this implementation.
