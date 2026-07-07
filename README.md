# The Supercycle — Kalshi Fee Dashboard

A static dashboard that estimates Kalshi's exchange fee revenue over time. A
Python collector reconstructs daily fee totals from public Kalshi market data
and writes them to CSV; a zero-build browser app reads those CSVs and renders
the stats and charts. There is no backend server and no database — GitHub
Actions refreshes the data on a schedule and GitHub Pages serves the static
site.

## How it all fits together

```
scripts/kalshi_fee_calculator.py   → talks to the Kalshi API, computes fees,
                                      writes kalshi_fee_daily.csv / _monthly.csv
scripts/merge_fee_csv.py           → merges a fresh incremental run into the
                                      committed CSVs without losing history
scripts/run_full_scan.sh           → resumable wrapper for a full-history,
                                      checkpointed local/VPS scan
.github/workflows/update-data.yml  → runs the collector hourly, merges the
                                      output, commits the CSVs to main
.github/workflows/deploy-pages.yml → publishes the repo to GitHub Pages after
                                      main updates
index.html / app.js / src/data.js  → the browser dashboard; fetches
                                      kalshi_fee_daily.csv and renders it
```

## 1. Run the website locally

The site is fully static — any file server works.

```bash
python3 -m http.server 8000
```

Open `http://localhost:8000`. `app.js` fetches `./kalshi_fee_daily.csv` from
the project root; if that file is missing or empty, the page shows an empty
state with an "Upload CSV" control so you can load one manually.

## 2. Generate fee data

Install the one Python dependency:

```bash
pip install -r requirements.txt
```

Run the collector from the project root so it writes CSVs beside
`index.html`:

```bash
python3 scripts/kalshi_fee_calculator.py --output-dir .
```

This hits the public Kalshi API directly (no auth) and can take a long time
for a full-history run because historical markets are fetched one at a time
with a rate-limit delay. Useful flags:

```bash
# Only include the last N days (fast incremental refresh)
python3 scripts/kalshi_fee_calculator.py --days 90 --output-dir .

# Skip perpetual futures entirely
python3 scripts/kalshi_fee_calculator.py --skip-perps --output-dir .

# Skip the open/live market pass — historical markets only
python3 scripts/kalshi_fee_calculator.py --skip-live --output-dir .

# Resumable full-history scan with a checkpoint file
python3 scripts/kalshi_fee_calculator.py \
  --checkpoint data/checkpoints/scan.json --resume --output-dir .

# Exit with code 2 if the scan looks incomplete/biased (see "Scan quality
# checks" below) — useful for CI/cron loops
python3 scripts/kalshi_fee_calculator.py --resume --fail-on-incomplete
```

It writes two files:

- `kalshi_fee_daily.csv` — one row per UTC date, with `daily_fee`,
  `cumulative_fee`, and one `cat_<category>` column per market category seen
  in that run (columns are generated dynamically, so new categories just
  appear).
- `kalshi_fee_monthly.csv` — `month`, `fee` rollup of the daily file.

### Bootstrapping a full-history scan (`run_full_scan.sh`)

A full-history collector run can take hours and may hit transient API
errors. `scripts/run_full_scan.sh` wraps the collector in a retry loop that
resumes from a checkpoint until a complete, quality-checked scan finishes:

```bash
./scripts/run_full_scan.sh
# or override defaults:
CHECKPOINT=data/checkpoints/scan.json OUTPUT_DIR=. LOG=logs/full-scan.log \
  ./scripts/run_full_scan.sh
```

It calls the collector with `--resume --fail-on-incomplete` in a loop,
sleeping 60s after a "scan incomplete" exit (code 2) and 120s after any other
error, and logs everything to `logs/full-scan.log`. It exits 0 only once a
complete run passes the scan quality checks.

On a VPS (2 GB RAM is plenty), run it inside `tmux` or `screen` so SSH
disconnects don't kill the job:

```bash
git clone https://github.com/ManiFed/KalshiFees.git && cd KalshiFees
tmux new -s kalshi
./scripts/run_full_scan.sh
```

A full bootstrap typically takes several days (200k+ historical markets plus
the live pass). When it finishes, commit and push the generated CSVs to
`main` so GitHub Pages picks them up.

### Merging incremental refreshes (`merge_fee_csv.py`)

Re-running the full collector every time would be slow and would re-fetch
years of settled history that never changes. `scripts/merge_fee_csv.py` takes
a small incremental CSV (e.g. the last 45 days) and merges it into the
existing, committed `kalshi_fee_daily.csv`:

```bash
python3 scripts/merge_fee_csv.py \
  --existing kalshi_fee_daily.csv \
  --incoming /path/to/fresh_kalshi_fee_daily.csv \
  --output-dir .
```

Rows are merged by date — incoming rows overwrite existing rows for the same
date (so recent, previously-partial days get corrected), older history is
left untouched, and `cumulative_fee` plus `kalshi_fee_monthly.csv` are
recomputed from the merged result.

## 3. Automated data refresh (GitHub Actions)

`.github/workflows/update-data.yml` runs hourly (`cron: '0 * * * *'`) and can
also be triggered manually with `workflow_dispatch` inputs:

- `refresh_days` (default `45`) — how many days of history to re-pull.
- `full_history` (default `false`) — if `true`, skip the `--days` filter and
  run the collector across all history (slow; may hit the job's timeout).

The job installs `requirements.txt`, runs the collector into a temp
directory with `--skip-perps` (perp fees are reported separately and don't
need re-pulling every hour), merges the result into the committed CSVs with
`merge_fee_csv.py`, and pushes a commit if anything changed. It has a
360-minute timeout and uses `concurrency: update-data` with
`cancel-in-progress: true` so overlapping runs don't collide.

`.github/workflows/deploy-pages.yml` redeploys the static site to GitHub
Pages whenever `main` changes (including after the data workflow commits) or
on a manual `workflow_dispatch`. It uploads the whole repo as the Pages
artifact — no build step, since the site is static HTML/CSS/JS.

## 4. Fee calculation methodology

The collector (`scripts/kalshi_fee_calculator.py`) works in five steps:

1. **Load fee schedule** — `GET /series/fee_changes?show_historical=true`
   gives each series' fee multiplier and `fee_type` over time;
   `GET /series/{ticker}` is a fallback for series missing from that log.
   `KXBTCY`, `KXCITRINI`, and `KXDOED` are hardcoded as zero-fee.
2. **Enumerate markets** — open markets via `GET /markets`
   (`status=open`, `mve_filter=exclude`, activity filter), closed markets via
   paginated `GET /historical/markets` (200k+ rows; resumable via
   checkpoint cursor).
3. **Fetch candlesticks** — markets open under 24 hours use 1-minute
   candles; longer-lived markets use daily candles. Both live (batched, up
   to 100 tickers per call) and historical candle fetches include a 6-hour
   buffer around each market's open/close time.
4. **Compute fees per candle** — for each candle, look up the fee schedule
   in effect *at that candle's own timestamp* (not a single global cutoff
   date), then apply:
   - `fee_type = quadratic` → taker-only:
     `ceil(0.07 × mult × P × (1−P), ¢) × contracts`
   - `fee_type = quadratic_with_maker_fees` → **both** taker and maker pay
     in full on every fill (there's no assumed volume split between them):
     taker leg as above, plus a maker leg at `0.0175 × mult` (25% of the
     taker rate). Sports series began rolling onto this fee type from
     roughly October 2025, per series, per Kalshi's fee-change API — not on
     a single fixed date. MVE sports combo series absent from that log
     (`KXMVESPORTSMULTIGAME*`, `KXMVECROSSCATEGORY`) are mapped to maker
     fees post-October 2025 as well.
   `P` is the candle's mean traded YES price (`mean_dollars`, or a legacy
   `mean`/`close` field as fallback). Fees are accumulated into a
   `daily_series[date][category]` map.
5. **Perpetual futures** — a separate `/margin/markets` rail. Revenue is
   estimated as default maker/taker basis-point rates (2 bps / 5 bps) on
   notional volume. Funding-rate flows move between traders, not to Kalshi,
   and are excluded. Because no daily perp volume history is available yet,
   perp fees are reported as an **all-time total**, not folded into the
   daily series.

Output: console tables (daily fees, monthly summary, all-time totals,
trailing-30-day annualized run rate, a fee-multiplier sensitivity table) plus
`kalshi_fee_daily.csv` and `kalshi_fee_monthly.csv`.

### Known approximations

- Candle mean price is a midpoint proxy, not a per-fill VWAP.
- `ceil()` rounding is applied per candle bucket, not per fill — a slight
  undercount versus per-fill rounding on small trades.
- Series absent from the fee-change log default to taker-only before the
  per-series maker-fee rollout.
- The zero-fee series list (`KXBTCY`, `KXCITRINI`, `KXDOED`) may be
  incomplete if the API omits others.
- Perp fee bps are hardcoded defaults; authenticated actual rates aren't
  queried (the collector makes unauthenticated requests only).
- The 6-hour candle buffer may still miss pre-open activity on some markets.

### Scan quality checks

`validate_scan_quality()` flags a run as suspect if historical pagination
didn't finish, live markets were skipped, fewer than 150k historical markets
or 30 distinct fee dates were seen, the earliest fee date looks too recent
(likely a partial/recency-biased scan), or the average fee per market is
implausibly low. Pass `--fail-on-incomplete` to turn these warnings into a
non-zero exit code (used by `run_full_scan.sh` to trigger a retry).

### Resumable checkpoints

`--checkpoint <path> --resume` persists progress (live/historical pass
completion, pagination cursor, last-processed ticker within the current
page, running totals, the daily series so far) to a JSON file after every
historical page and every 200 markets, so a long full-history scan can be
killed and resumed without starting over or double-counting fees.

## 5. The dashboard (browser)

`index.html` loads `app.js` (a JS module) and `styles.css`. `app.js` fetches
`kalshi_fee_daily.csv`, parses it with the small CSV parser in
`src/data.js`, and derives everything the page shows:

- **Cadence** (daily / weekly / monthly) and **window** (since first trade /
  last year / last 6 months / last month) are picked from the two dropdown
  menus in the page header; `src/data.js` re-groups and re-filters the rows
  client-side on every change — no server round trip.
- **Headline stats** — total fees in the window, average fee per period,
  trailing-30-day annualized run rate, and the single highest fee day.
- **Charts** (drawn directly on `<canvas>`, no charting library) — fee
  accumulation (bars + cumulative line), the leading category's trend, a
  monthly fee-pace area chart, and a category fee-mix bar chart. Category
  charts are driven entirely by whatever `cat_*` columns exist in the CSV.
- If the CSV can't be fetched (e.g. running before any collector run), the
  page shows an empty state and reveals an "Upload CSV" control so you can
  load a local file instead.

`src/data.js` holds only pure data-transformation functions (CSV parsing,
filtering, grouping, metrics, formatting) — kept dependency-free and
testable in isolation from the DOM code in `app.js`.

## 6. Tests

Python tests (fee formulas, checkpointing, CSV merge, scan-quality
validation) and a Node test for the browser-side data module:

```bash
python3 -m unittest discover -s tests -p "test_*.py"
npm test   # runs `node --test tests/data.test.mjs`
```

- `tests/test_fee_schedule.py` — fee formula correctness per era, per fee
  type, and candle price extraction.
- `tests/test_collector_regressions.py` — series-ticker resolution, output
  directory handling, perp/daily-series separation, checkpoint round-trips,
  scan-quality warnings.
- `tests/test_merge_fee_csv.py` — merge behavior for `merge_fee_csv.py`.
- `tests/data.test.mjs` — CSV parsing, filtering, grouping, and metrics in
  `src/data.js`.

## Repo layout

```
index.html, styles.css, app.js   Dashboard UI and rendering logic
src/data.js                      Pure data-transform functions used by app.js
scripts/kalshi_fee_calculator.py Fee collector (talks to the Kalshi API)
scripts/merge_fee_csv.py         Merges incremental collector output into repo CSVs
scripts/run_full_scan.sh         Resumable wrapper for a full-history scan
tests/                           Python + Node test suites
.github/workflows/               Scheduled data refresh + Pages deploy
kalshi_fee_daily.csv / _monthly.csv  Collector output consumed by the dashboard
AUDIT.md                         Point-in-time audit notes on the collector script
```

Keep the Python collector in `scripts/`, not `src/` — `src/` is browser
JavaScript only.
