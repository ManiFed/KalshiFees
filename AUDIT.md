# Kalshi Fee Collector Audit

Original audited file: `/Users/eligoldfine/Downloads/kalshi_fee_calculator (3).py`

Project copy: `/Users/eligoldfine/Documents/kalshifee/scripts/kalshi_fee_calculator.py`

Audit date: June 28, 2026

## Summary

The collector has a clear architecture and is directionally sound for producing a daily estimated fee-revenue series:

1. Load per-series fee multiplier changes from `GET /exchange/series-fee-changes`.
2. Enumerate live event markets from `GET /markets` with `status=open` and `status=active`.
3. Enumerate closed markets from `GET /historical/markets`.
4. Fetch candlesticks for each market, using 1-minute candles for markets open under 24 hours and daily candles for longer-lived markets.
5. Estimate event contract fees candle by candle with an era-aware fee formula.
6. Estimate perpetual futures fees from `/margin/markets`.
7. Write event-contract daily and monthly CSV outputs, while reporting perpetual futures as a separate all-time adjustment until daily perp history is available.

The project copy fixes the highest-friction local issues: it writes output to a configurable `--output-dir`, uses API `series_ticker` where available, dedupes live markets, removes unused auth flags, labels the sensitivity table as approximate, and keeps lifetime perp estimates out of the daily event-contract series. The remaining risks are authenticated access, perp daily history, runtime/caching, and completeness issues that can change the estimate materially.

## What Looks Good

- The script applies the pre-May-2025 taker-only fee era and the May-2025-plus maker/taker era per candle rather than per market. That is the right boundary for markets spanning the fee change.
- `price` extraction uses explicit `None` checks, which avoids accidentally skipping a legitimate `0` value because it is falsy.
- The fee multiplier history is sorted by effective timestamp and consulted before hardcoded fallbacks.
- Short-lived markets use 1-minute candles, reducing undercount risk for sports and fast event markets.
- Historical candle requests include a six-hour open/close buffer.
- The script separates event contracts from perpetual futures and correctly excludes funding flows from revenue.
- CSV category columns are generated dynamically, which lets the dashboard support categories not known in advance.

## High-Priority Issues

### 1. Series ticker can be derived incorrectly

Status: fixed in the project copy at `scripts/kalshi_fee_calculator.py` and the audited Downloads script.

In both live and historical processing, the script computes:

```python
series = ticker.split("-")[0].upper()
```

For live markets, the API response already includes `series_ticker`, but the accumulator does not use it. For historical markets, the script does not read a `series_ticker` field if one is present. If a market ticker prefix differs from its actual fee series identifier, the wrong fee multiplier or zero-fee status can be applied.

Recommended fix:

```python
series = (meta.get("series_ticker") or ticker.split("-")[0]).upper()
```

For historical markets:

```python
series = (m.get("series_ticker") or ticker.split("-")[0]).upper()
```

### 2. Output paths are not portable on this machine

Status: fixed in the project copy at `scripts/kalshi_fee_calculator.py` and the audited Downloads script with `--output-dir`.

The script writes to:

```python
daily_path = "/mnt/user-data/outputs/kalshi_fee_daily.csv"
monthly_path = "/mnt/user-data/outputs/kalshi_fee_monthly.csv"
```

That path is common in some notebook/container environments, but it does not naturally exist in this local macOS workspace. The script can complete the expensive API work and then fail while writing output.

Recommended fix: add an `--output-dir` argument defaulting to the current directory, create the directory with `os.makedirs(output_dir, exist_ok=True)`, and write the CSVs there.

### 3. Perpetual futures are attributed to the run date

Status: fixed in the project copy at `scripts/kalshi_fee_calculator.py` and the audited Downloads script by excluding lifetime perp estimates from `daily_series` and reporting them as an all-time adjustment.

Before the fix, `process_perps()` attributed all detected perp volume to `today`. That was useful as a rough total estimate but made the daily series misleading, especially if historical perp volume was included in market lifetime fields.

Recommended fix: fetch perp candle or daily volume history if Kalshi exposes it. If not available, keep perps out of the daily chart and report them as a separately dated all-time adjustment.

### 4. Auth arguments are accepted but not used

Status: fixed in the project copy at `scripts/kalshi_fee_calculator.py` and the audited Downloads script by removing `--api-key`, `--private-key-path`, and the unused `process_perps()` parameters.

Before the fix, the CLI accepted `--api-key` and `--private-key-path`, and `process_perps()` received them, but the HTTP session never signed requests or attached authentication headers. Any authenticated-only fee tier or perp detail remained unavailable.

Recommended fix: either implement authenticated request signing or remove the arguments to avoid implying they affect results.

## Medium-Priority Issues

### 5. Live market statuses can duplicate markets

Status: fixed in the project copy at `scripts/kalshi_fee_calculator.py` and the audited Downloads script by storing live markets in a dict keyed by ticker.

The script paginates both `status=open` and `status=active` into `live_markets` without deduping by ticker. If the API returns the same market under both statuses, it can double-count.

Recommended fix: store live markets in a dict keyed by ticker.

### 6. Fee change lookup is linear per candle

Status: fixed in the project copy at `scripts/kalshi_fee_calculator.py` and the audited Downloads script with `bisect_right` over sorted effective timestamps.

`taker_mult_at()` scans each series' fee changes from the beginning. This is probably fine for a small change log, but if the log grows, repeated per-candle lookup becomes unnecessary overhead.

Recommended fix: use `bisect` over sorted effective timestamps.

### 7. Monthly sensitivity calculation overstates precision

Status: fixed in the project copy at `scripts/kalshi_fee_calculator.py` and the audited Downloads script by labeling the table approximate.

The sensitivity table scales the grand total using the current standard taker/maker constants:

```python
base_rate = STANDARD_TAKER * base_tf + STANDARD_MAKER * (1 - base_tf)
```

That does not preserve zero-fee series, reduced-fee series, pre-maker-fee history, or per-candle rounding. It is fine as a rough directional table, but it should be labeled as approximate.

### 8. Category inference is incomplete by design

The fallback prefix map is useful, but it will age as Kalshi adds new series. Unknown or newly named markets fall into `other`.

Recommended fix: prefer API category fields and periodically audit the `other` bucket.

## Low-Priority Issues

### 9. Historical API pagination ceiling can silently truncate data

The script warns when `MAX_PAGES` is hit, but still produces output. That is reasonable, but the CSV should include a metadata note or the console should make the output status unmistakable.

### 10. Runtime can be very long

Historical markets are fetched one by one with a delay. This is safer for rate limits but can take a long time for all-time history.

Recommended fix: add resumable local caching keyed by market ticker, period, start timestamp, and end timestamp.

### 11. Candle volume field assumptions should be verified

The code prefers `volume_fp`, then `volume`. That is sensible, but the meaning of `volume_fp` versus `volume` should be rechecked against the current Kalshi API schema before treating the estimate as production-grade.

## Recommended Patch List

1. Use API `series_ticker` wherever available.
2. Add `--output-dir` and create the target directory before writing CSVs.
3. Dedupe live markets by ticker.
4. Either implement authenticated signing or remove unused auth flags.
5. Separate perp all-time estimate from daily event time series until daily perp history is available.
6. Label maker/taker sensitivity as approximate.
7. Future enhancement: add cache/resume support for historical candles.
8. Future enhancement: add a metadata JSON output with collector version, run timestamp, taker fraction, min date, API warnings, skipped markets, and known approximations.

## Syntax Check

The collector should be syntax-checked with:

```bash
python3 -m py_compile "/Users/eligoldfine/Downloads/kalshi_fee_calculator (3).py"
```

This checks that the script parses, but it does not prove API correctness or estimate accuracy.
