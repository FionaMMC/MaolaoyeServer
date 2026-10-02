# Execution experiment — fixed design before results

Question: with the same existing strategy signals, which execution rule improves price accessibility without sacrificing net strategy returns?

1. Reconstruct Hydra's existing monthly target weights, using unadjusted ETF OHLC and corporate actions. Generate trades no earlier than the trading session after a target timestamp. No new stock selection or weight optimization.
2. Compare frozen previous-close limits (one and three sessions), next-open protected limits with adverse overnight-gap caps of 50/100/200 bp (one and three sessions), and uncapped next-open execution as a cost-sensitive reference. All open fills include modeled adverse execution costs; all daily touch fills are explicitly optimistic bounds. No claim that OHLC proves a fill.
3. Evaluate full history and 2025 onward as a chronological execution check, not a clean out-of-sample strategy validation (the weights originate from prior research). Report net return, drawdown, turnover, intended-notional completion, and open versus touch dependence. Use model capital 200,000 and 1,000,000 yuan, ETF 100-share lots, 1 bp commission with 5 yuan minimum, 1% cash reserve. Apply identical assumptions to every policy.
4. Separate overnight price drift (signal close to arrival) from spread/impact (arrival to execution); test 5/25/50 bp adverse execution costs. Futures hedges are outside this ETF experiment.
5. For actual active stock/ETF strategies, compute read-only aggregate order status and next-session price-accessibility statistics on the server. Do not export raw account/order records. Do not confuse eligibility, fetching, queue fills, or pending work with price failure.
6. Probe minute-data availability. If unavailable, state the limitation and do not label a daily-OHLC proxy TWAP/VWAP or realized fill rate. Small-cap full NAV counterfactual requires historical targets and tradable minute/auction data; avoid inventing either.

No production execution policy changes or trading are included in this experiment.

## Recorded follow-up checks

- The stock minute sample (000001.SZ) was readable, but all 149 actual symbols returned permission code 40203. Switched the order experiment to provider raw daily data; first tested a fixed SHA256 sample of 64 V20H symbols, then expanded to all July–September orders (347 unique symbols) to eliminate sampling instability. Both aggregate outputs are retained.
- Actual order limits revealed that several strategies already have approximately 50 bp frozen buffers. Added frozen-50-bp touch scenarios to the Hydra replay as a required current-policy comparison; zero-bp frozen close alone is not an accurate current Hydra baseline.
- Added common initial holdings across policies and exploratory paired 3-month-block bootstrap as post-result robustness checks. These are not new alpha/weight optimizations or clean out-of-sample tests.
