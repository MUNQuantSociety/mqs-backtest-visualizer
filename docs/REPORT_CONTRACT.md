# Backtest report contract

This describes reports produced by the current implementation. It does not
assert a production deployment or certify historical runs. Existing persisted
reports keep their original values; metadata may be absent on older runs.

The API models are in [src/schemas/backtests.py](../src/schemas/backtests.py),
application calculations in [src/services/reporting.py](../src/services/reporting.py),
and persistence in [src/repositories/reports.py](../src/repositories/reports.py).
Only successful reports are saved, as one JSONB document with no stored status.
See [Completed report storage](COMPLETED_REPORT_STORAGE.md) for the current
schema, temporary job polling, ownership, and migration behavior.
See [Architecture Flow](ARCHITECTURE_FLOW.md) for the request/worker/database
design and [README](../README.md) for startup, storage and deployment.

## Public report and availability

`GET /api/backtests/{id}` returns the existing camelCase detail shape with these
additive fields:

| Field | Meaning |
| --- | --- |
| `metrics.unavailable` | Map of metric names, such as `sharpe` or `profitFactor`, to a reason the value is undefined. |
| `reportMetadata` | Persisted calculation conventions, benchmark provenance, capital baseline and run diagnostics. |
| `openPositions` | Supplemental final marks and unrealized P&L for still-open FIFO lots. |

The existing metric properties remain numeric for client compatibility.
Undefined metrics use zero placeholders plus an entry in `metrics.unavailable`
in the saved JSON and API response. A client must consult that map before displaying a
zero as a measured result. `totalTrades: 0` is a valid count; an empty
`unavailable` map means the represented metrics are defined. Queued/failed
status must also be checked before treating summary placeholders as results.
List summaries do not have a metric availability map; use the detail report
for availability-aware presentation of completed-run metrics.

Ratios remain ratios: `totalReturn: 0.1` means 10%; drawdown is nonpositive.
JSON uses null for unavailable benchmark values and marks. No NaN or infinity
is a public metric value. Older rows without provenance are not evidence that
their statistics used the conventions below.

## Dates, initial capital and final equity

The engine labels equity and benchmark samples with New York calendar dates.
Aware timestamps are converted to `America/New_York`; naive date labels are
interpreted there. Keep `MARKET_TIMEZONE=America/New_York` for matching fill
dates. The last available observation need not be a scheduled exchange close.

The raw `RunResult.equity_curve` starts with an explicit initial-capital point
on the **same date as the first observed sample**. Its first two points therefore
share a date; event mode can have more points on that date. The worker keeps
the last point on each date, in date order, so the public curve has one row per
observed date and does not retain that initial-capital point as an extra row.
No synthetic prior date, holiday, weekend or missing session is inserted.

`reportMetadata.equity.baseline` preserves the capital value/date and the kind
`initial_capital_before_trading`. The worker removes the raw engine's
`curve_index` and writes `includedInDailyCurve: false`; the public metadata has
no baseline curve index. A client may annotate the baseline separately. It must
not shift it to the previous date, count it as an additional daily observation,
or replace the first actual daily value with it.

Daily-last persistence is the intended handling: no extra worker baseline row
is required. First-session P&L remains included in capital-based total return,
even when the first stored daily equity differs from `initialCapital`. The
daily risk measures below deliberately start from that first observed close.

The event runner also records the final processed bar when it falls after the
last strategy poll. This samples the executor without causing another strategy
decision, and includes price changes and any intervening fills. The worker
checks the engine's final equity against the final daily observation before
persisting completion; `finalEquity` is the ending stored equity.

## Application metric conventions

Let `E[0] ... E[n-1]` be the observed daily-last equities and `C` the initial
capital. Daily returns are `E[i] / E[i-1] - 1`; a pair with a zero previous
equity is excluded from the return series. Annualization uses 252 periods and
an annual risk-free rate of 0.02. Excess returns subtract `0.02 / 252`.

| Metric | Implemented basis |
| --- | --- |
| `totalReturn` | `E[n-1] / C - 1`, including first-session P&L. |
| `cagr` | `(E[n-1] / E[0]) ** (252 / (n-1)) - 1`; requires at least two observations, positive first equity and nonnegative last equity. |
| `volatility` | Sample standard deviation of observed daily returns, multiplied by `sqrt(252)`. |
| `sharpe` | Mean daily excess return / sample standard deviation of excess returns, multiplied by `sqrt(252)`. |
| `sortino` | Mean daily excess return / square root of the mean squared **negative excess returns only**, multiplied by `sqrt(252)`. |
| `maxDrawdown` | Minimum `equity / running_peak - 1` over observed daily closes; the peak starts at the first close, not initial capital. |
| `winRate` | Number of profitable closed FIFO lots / all closed FIFO lots. |
| `profitFactor` | Sum of positive closed-lot P&L / absolute sum of negative closed-lot P&L. |
| `totalTrades` | Number of closed FIFO lots, including lots produced by partial exits. |

Volatility, Sharpe and Sortino require at least two valid daily returns.
Zero dispersion makes Sharpe undefined; no negative excess returns makes
Sortino undefined. No closed lots makes win rate undefined; no closed losses
makes profit factor undefined. Overflowing, non-finite or out-of-range metric
values remain unavailable, rather than becoming infinities. A flat series can
have defined zero volatility while Sharpe remains undefined.

These application calculations do not change strategy execution. Legacy engine
metrics remain under `reportMetadata.engineMetrics` for diagnosis and can differ
from the public daily statistics. Diagnostic engine CSVs are not the source of
the API/export metric contract.

## Buy-and-hold benchmark

`reportMetadata.benchmark.kind` is `configured_universe_buy_and_hold`.
The benchmark uses the configured ticker universe and its explicit weights;
only absent weights imply an equal-weight default. Mapping keys outside the
universe are ignored. Omitted/zero allocations remain cash, and weights are
not renormalized. Ordered weight lists must match the universe; malformed,
negative or non-finite weights fail clearly.

Each allocation buys fixed shares at that ticker's first valid close inside
the run window. Strategy lookback prices are excluded. A delayed ticker's
allocation remains cash until its first valid quote; a ticker with no quote
remains cash throughout. After entry, its last observed close is carried
forward for valuation. The benchmark adds no dividend payments, fees, interest
or rebalancing; it uses the close prices supplied by the engine. If weights
sum above one, residual cash is negative financing with no interest charge.

Benchmark marks are built on observed timestamps, without a calendar-minute
grid or future-price backfill. Each engine performance sample receives the
benchmark known as of that timestamp, before daily-last downsampling. Prices
after the final performance observation are excluded. The benchmark starts
at initial capital at its first observation; the first **public daily** point
can differ after an intraday move. Unavailable benchmark values remain null.

Provenance fields include:

| Metadata key | Meaning |
| --- | --- |
| `weights`, `universe`, `initial_value`, `cash_weight` | Actual allocations, ticker ordering, starting capital and residual allocation. |
| `entry_rule`, `missing_price_policy`, `rebalanced`, `costs_included` | Valuation assumptions. |
| `timezone`, `first_observation`, `last_observation` | Calendar convention and observed benchmark endpoints. |
| `entry_timestamps`, `last_price_timestamps` | Per-ticker entry and final known quote times; final quotes may be stale. |
| `missing_tickers`, `delayed_tickers` | Allocations without an entry or entering after the benchmark's first observation. |
| `coverage`, `coverage_scope` | `complete` or `partial` refers to **in-window entry availability**, not a guarantee that all interior bars exist. `unavailable` means no benchmark could be constructed. |
| `observed_price_rows`, `observations_per_ticker` | Counts of usable observed quotes. |
| `price_sampling` | `engine_observed_bars` for event mode, `daily_close` for fast mode. |

When coverage is `unavailable`, fewer provenance fields may be present. Clients
must not infer missing fields or silently present a partial universe as complete.
The market-data coverage endpoint separately reports stored ticker date bounds;
those bounds also do not establish continuous interior data coverage.

Fast mode reuses the existing vector adapters and daily-price fetch. It remains
a strategy approximation with warmup positions and the existing first-day
return behavior; it is not event-mode parity. The built-in `VolMomentum`,
`MomentumStrategy` and `RegimeAdaptiveStrategy` have adapters;
`portfolio_dummy` does not. Unsupported strategies fail before loading data.
Fast output has no executed fills or final position marks.

For diagnostic artifacts, the root `benchmark_buy_and_hold.csv` is the
configured-weight comparison. The vector engine can also write
`benchmark_buy_and_hold_performance.csv` in a subdirectory; that is its legacy
compounded equal-weight comparison and must not replace the public benchmark.

## Execution controls

The API separates these reserved browser controls from strategy parameters in
`params`; other keys still require a matching strategy parameter specification.

| Control | Implemented mapping |
| --- | --- |
| `slippageBps` | `RunRequest.slippage = slippageBps / 10000`; 5 bps is `0.0005`. Event fills buy above/sell below the observed price. No legacy `CostModel` replaces this explicit request value. |
| `commissionPerShare` | `RunRequest.commission_per_share`; cash commission per filled share on **both** buys and sells. Event mode reserves affordability for fees and records them separately as fill `fees`. |
| `universe` | The same registry ticker set preserves configured weights. An explicitly changed set becomes `TICKERS` with equal `WEIGHTS`; coverage is checked against that requested universe. |
| `signals` | Empty `signals: []` is accepted. Nonempty signal overrides are unsupported and rejected. |
| `sentimentGate` | `{enabled: false}` stores nothing. `{enabled: true, threshold}` with `threshold` in [-1, 0] needs event mode and `NEWS_POSTGRES_*` (else 422). The worker loads the universe's scores from the live `news_sentiment` table read-only into `RunRequest.sentiment_gate`; a load failure fails the run. At each bar, a target that would add long exposure is capped at the current long (or flat when short) while the ticker's 7-day mean score of articles available strictly before the bar is below the threshold. An article is available 5 hours after `published_at`, or the next day for a date-only (midnight) stamp; no articles scores 0.0. `reportMetadata.sentimentGate` records `enabled`, `threshold`, `window`, `blockedEntryCount` and per-ticker `coverage` (`articleCount`, `firstAvailable`); an ungated run records `{enabled: false}`. |

Omitting cost controls preserves the engine's legacy zero defaults. The browser
form sends 5 bps and $0.005/share unless changed; explicit zero is respected.
Fast mode rejects nonzero per-share commission. With commission explicitly zero,
fast slippage remains the existing daily weight-turnover approximation, not
simulated per-fill execution.

`reportMetadata.executionCosts` records `slippageFraction`, `commissionPerShare`,
`commissionBasis: cash_per_filled_share_each_side`, `slippageBasis` (`fill_price`
or `daily_weight_turnover`) and `legacyCostModel: false`. The buy-and-hold
benchmark remains cost-free. Strategy target calculations are unchanged;
execution costs affect affordable quantities, settlement cash and ending equity.

## Trades, final marks and fees

`trades` are FIFO lots derived from engine fills, not a second execution log.
Closed long P&L is `(exitPrice - entryPrice) * quantity`; shorts reverse that
sign. Partial exits can produce several closed rows from one opening fill.
Open lots retain null exit fields and zero **realized** `pnl`/`returnPct`.

`openPositions` reports `tradeSeq`, `symbol`, `side`, `quantity`, `entryPrice`,
`markPrice`, `unrealizedPnl`, signed `marketValue` and `fees`. `tradeSeq`
corresponds to the lot sequence used in `trades[].id` (`<run-id>:<seq>`).
The engine's internal `final_prices` supplies marks used in the ending equity
sample; it is not a fabricated closing fill. An absent/nonpositive/non-finite
mark produces null unrealized P&L and market value. A valid last quote may be
older than the final sample.

Realized and supplemental unrealized P&L are gross of separately reported fees.
For a closed lot, net P&L is `pnl - fees`; FIFO pairing allocates the opening
and closing cash commissions to the relevant lots. Engine slippage/costs already
embedded in fill prices remain there and must not be subtracted again. Do not
assert `initialCapital + sum(trades[].pnl) == finalEquity`: open exposure and
fee treatment must be accounted for, and stored values can be rounded. Do not
add unrealized P&L to final equity; equity already includes the marked position.

## Metadata persistence

The worker merges `RunResult.report_metadata` into `RunMetrics.extra`, alongside
`calculation_metadata()`, `engineMetrics`, open-position marks and diagnostics.
The detail API exposes that object as `reportMetadata` except `openPositions`,
which is returned separately. Engine dictionaries retain their snake_case keys;
the API does not recursively rename dictionary keys to camelCase.

`reportMetadata.execution` contains `fillCount` and a nullable `message`.
Completed event runs with no fills explain whether the strategy had insufficient
warmup, generated no orders, or requested orders without fills. Fast runs explain
that their vectorized positions do not produce an individual fill table. Clients
should show the message on completed runs, without inferring a failure from an
empty trade list. VolMomentum also records bounded `strategyDiagnostics`: signal
evaluation/skip/request counts and each ticker's strongest momentum-to-threshold
snapshot. Its threshold remains 1.5 times annualized daily volatility; momentum
and volatility are both expressed in percent. These fields are absent on older
reports, whose stored results are unchanged.

Calculation keys include `reportVersion`, `frequency`, `periodsPerYear`,
`annualRiskFreeRate`, `standardDeviation`, `riskReturnBasis`, `totalReturnBasis`,
`cagrBasis`, `tradePnlBasis` and `openPositionPnlBasis`. Diagnostics include
`fill_count`, `closed_trades`, `open_lots`, `equity_samples`,
`equity_points_stored`, `mode` and `marketTimezone`.
`equity_samples` includes the engine baseline; `equity_points_stored` does not
count it as a separate observation. Metadata is additive; consumers should
tolerate absent keys on older records and new keys in future reports.

New worker runs also include `strategySource`: `backend`, `storageKey`,
`sourceSha256` and `configSha256` for a downloaded strategy package. Hashes
describe the exact downloaded bytes before execution; run-specific controls
are separate and do not alter stored source/config. Legacy local built-ins
instead report `backend: builtin` and `classPath`. This field is optional on
older reports and is not a security signature or S3 version lock. See
[S3 strategy flow](S3_STRATEGY_FLOW.md).

## Downloads

`GET /api/backtests/{id}/exports/{filename}` accepts exactly:

| Filename | Columns/content |
| --- | --- |
| `equity.csv` | `date,equity,benchmark` from persisted daily observations. |
| `trades.csv` | `id,symbol,side,entryDate,exitDate,entryPrice,exitPrice,quantity,pnl,returnPct,fees`. Open lots keep empty exit fields. |
| `metrics.csv` | `metric,value,unavailableReason`; metric rows plus `initialCapital` and `finalEquity`. Undefined values are empty with a reason. |
| `report.json` | The full camelCase detail report, including `metrics.unavailable`, `reportMetadata` and `openPositions`. |

Only completed runs can be exported: unfinished/failed runs return 409, and
unknown filenames or runs return 404. CSV nulls are empty fields; numeric
negative returns/P&L remain numbers. Formula-like text is prefixed with an
apostrophe for spreadsheet safety. Responses set attachment disposition,
`Cache-Control: private, no-store`, and `X-Content-Type-Options: nosniff`.

Exports are generated from persisted report records, not arbitrary paths or
ephemeral worker CSVs. An API/worker restart alone does not remove them; deleting
the run does. There is no separate open-position CSV: use `report.json` for
supplemental marks. Browser clients must preserve the additive fields in their
schemas to use them.

## Verification commands

Run isolated checks with Python 3.12 and the existing virtual environment:

```powershell
.\venv\Scripts\python.exe -m pytest -q -m 'not db and not ci_db' tests/unit/test_engine_costs.py tests/unit/test_report_benchmark.py tests/unit/test_db_adapter.py tests/unit/test_reporting.py tests/unit/test_api_contract.py
```

On Linux/macOS replace the interpreter with `venv/bin/python`. The explicit
disposable-PostgreSQL (`ci_db`) and actual configured-database (`db`) procedures
are in [README: Tests](../README.md#tests). Neither is part of this isolated
command; opt in only after reviewing the intended database target.
