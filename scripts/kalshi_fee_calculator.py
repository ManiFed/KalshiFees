#!/usr/bin/env python3
"""
Kalshi Fee Revenue Calculator — v4
====================================
Estimates Kalshi's total fee revenue across:
  (A) Event contracts (binary markets + combos)
  (B) Perpetual futures

Outputs both a daily time series of estimated fees AND all-time/run-rate
totals. Fee structure changes over time are handled correctly per candle.

────────────────────────────────────────────────────────────────────────
FEE STRUCTURE HISTORY
────────────────────────────────────────────────────────────────────────

Kalshi's fee structure has changed twice in ways that materially affect
revenue calculations. The script applies different formulas to different
candle periods based on when the trading actually occurred.

  ERA 1: Launch (Oct 2021) → April 2025
    Only TAKERS paid fees. Makers paid nothing.
    Formula: fee = ceil(0.07 × P × (1-P) × contracts, nearest cent)
    Revenue per trade = taker_fee only

  ERA 2: April 2025 → present
    Both takers AND makers pay fees.
    Taker: ceil(0.07  × P × (1-P) × contracts, nearest cent)
    Maker: ceil(0.0175 × P × (1-P) × contracts, nearest cent)
    Revenue per trade = taker_fee + maker_fee (Kalshi collects both sides)

  Per-series multiplier changes:
    The API exposes GET /exchange/series-fee-changes which returns a
    timestamped log of every multiplier change per series (e.g. when INX
    markets got their multiplier halved to 0.035). The script fetches this
    log at startup and uses it to apply the correct multiplier to each
    candle period, rather than hardcoding known exceptions.
    Hardcoded fallbacks (INX/NASDAQ100 halved, zero-fee series) apply only
    when a series is absent from the fee change log.

────────────────────────────────────────────────────────────────────────
ARCHITECTURE
────────────────────────────────────────────────────────────────────────

Step 1 — Load fee change history
  GET /exchange/series-fee-changes → dict of {series_ticker: [(ts, multiplier)]}
  Sorted ascending by scheduled_ts so we can binary-search for the correct
  multiplier at any given candle timestamp.

Step 2 — Enumerate all markets
  Live:       GET /markets (status=open, status=active)
  Historical: GET /historical/markets
  Stored per market: ticker, series_ticker, category, open_time, close_time.

Step 3 — Fetch candlestick data per market
  Period selection:
    Markets with lifespan < 24 hours (live in-game sports) → 1-minute candles
    All others → daily candles
  Live markets use the batch endpoint (100 tickers/call, split by period).
  Historical markets are fetched individually (no batch endpoint available).
  6-hour buffer applied to historical start/end timestamps.

Step 4 — Compute fee per candle, accumulate daily time series
  For each candle:
    1. Determine which fee era the candle falls in (by end_period_ts)
    2. Look up the correct taker multiplier for this series at this timestamp
    3. Apply the era-appropriate formula:
         Pre-Apr 2025:  fee = ceil(taker_mult × P × (1-P), ¢) × contracts × 1.0
         Post-Apr 2025: fee = ceil(taker_mult × P × (1-P), ¢) × contracts × taker_fraction
                            + ceil(maker_mult × P × (1-P), ¢) × contracts × (1-taker_fraction)
    4. Bucket the fee into daily_series[YYYY-MM-DD] += fee

  This means a market that opened in March 2025 and closed in June 2025 will
  have its pre-April candles computed with taker-only fees and its post-April
  candles computed with the combined formula. No market is treated as if it
  existed entirely in one era.

Step 5 — Perpetual futures (separate /margin/ API rail)
  Launched May 29, 2026. Revenue = maker/taker bps on notional volume.
  Funding rate flows between traders, not to Kalshi — excluded.

Step 6 — Output
  • Daily time series table (date, daily_fee, cumulative_fee, category breakdown)
  • Monthly summary table
  • All-time total and trailing-30d annualized run rate
  • Sensitivity table for maker/taker split assumption
  • CSV: kalshi_fee_daily.csv and kalshi_fee_monthly.csv

────────────────────────────────────────────────────────────────────────
KNOWN APPROXIMATIONS
────────────────────────────────────────────────────────────────────────
  1. candle.mean is time-weighted midpoint avg, not fill-price VWAP
     Direction: unknown, magnitude small in liquid markets
  2. Maker/taker volume split assumed (default 70/30), not measured
     Direction: undercounts revenue if taker % is higher than assumed
  3. ceil() rounding applied per-market, not per-fill
     Direction: slight undercount vs per-fill rounding on small trades
  4. Fee change log covers per-series multiplier changes but not the
     global maker-fee introduction date (April 2025), which is hardcoded
  5. Zero-fee and reduced-fee series list may be incomplete
     Direction: slight overcount if any zero-fee series are missing
  6. Perp fee bps are defaults; actual authenticated rates are not queried
  7. 6h candle buffer may still miss pre-open activity on some markets

────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────
  pip install requests
  python kalshi_fee_calculator.py
  python kalshi_fee_calculator.py --taker-fraction 0.8
  python kalshi_fee_calculator.py --days 90          # limit to last 90 days
  python kalshi_fee_calculator.py --skip-perps
  python kalshi_fee_calculator.py --output-dir ./outputs
"""

import math
import csv
import time
import argparse
import requests
from bisect import bisect_right
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DELAY    = 0.12
MAX_PAGES = 2000

# ── Fee era boundary ──────────────────────────────────────────────────────────
# Confirmed by GWU academic paper (2026-001): maker fees introduced after April 2025.
# We use May 1 as the conservative boundary (gives April markets full taker-only treatment).
MAKER_FEE_START = datetime(2025, 5, 1, tzinfo=timezone.utc)

# ── Hardcoded fee constants (fallbacks if not in API fee change log) ──────────
STANDARD_TAKER = 0.07
STANDARD_MAKER = 0.0175
INX_TAKER      = 0.035
INX_MAKER      = INX_TAKER * 0.25

DEFAULT_TAKER_FRACTION = 0.70

# Zero-fee series: confirmed on Kalshi fee schedule page, June 2026
ZERO_FEE_SERIES = frozenset({"KXBTCY", "KXCITRINI", "KXDOED"})
INX_PREFIXES    = ("INX", "NASDAQ100")

INTRADAY_HOURS = 24

# Perp fee defaults
PERP_TAKER_BPS = 0.00050
PERP_MAKER_BPS = 0.00020


# ── HTTP helpers ──────────────────────────────────────────────────────────────

SESSION = requests.Session()

def get(path: str, params: dict = None, retries: int = 3) -> dict:
    url = BASE_URL + path
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=15)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                print(f"    [rate limit] sleeping {wait}s…", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                print(f"    [error] {url}: {e}", flush=True)
                return {}
            time.sleep(2 ** attempt)
    return {}

def paginate(path: str, result_key: str, params: dict = None):
    params = dict(params or {})
    params.setdefault("limit", 1000)
    pages = 0
    total = 0
    while True:
        data = get(path, params)
        items = data.get(result_key, [])
        if not items:
            break
        yield from items
        total += len(items)
        cursor = data.get("cursor", "")
        pages += 1
        if not cursor:
            break
        if pages >= MAX_PAGES:
            print(f"\n  ⚠ WARNING: pagination ceiling hit for {path} "
                  f"({total} items retrieved, results INCOMPLETE)\n", flush=True)
            break
        params["cursor"] = cursor
        time.sleep(DELAY)


# ── Timestamp / date helpers ──────────────────────────────────────────────────

def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

def ts_to_date(ts: int) -> str:
    """Unix timestamp → 'YYYY-MM-DD' string in UTC."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

def date_to_month(d: str) -> str:
    """'YYYY-MM-DD' → 'YYYY-MM'."""
    return d[:7]


# ── Fee change log ────────────────────────────────────────────────────────────

def load_fee_changes() -> dict[str, list[tuple[datetime, float]]]:
    """
    Fetch GET /exchange/series-fee-changes and build a lookup:
      {series_ticker: [(effective_datetime, taker_multiplier), ...]}
    sorted ascending by datetime so we can find the active multiplier
    at any given candle timestamp.

    fee_multiplier in the API response is an integer representing the
    multiplier × 10000 (i.e. 700 = 0.07, 350 = 0.035). We convert here.
    """
    data = get("/exchange/series-fee-changes")
    changes_raw = data.get("series_fee_change_arr", [])

    result: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for c in changes_raw:
        series  = c.get("series_ticker", "")
        mult_raw = c.get("fee_multiplier")
        ts_str  = c.get("scheduled_ts", "")
        if not series or mult_raw is None or not ts_str:
            continue
        dt   = parse_iso(ts_str)
        mult = float(mult_raw) / 10000.0  # convert integer basis to decimal
        if dt:
            result[series].append((dt, mult))

    # Sort each series list ascending by effective date
    for series in result:
        result[series].sort(key=lambda x: x[0])

    print(f"  Loaded {len(changes_raw)} fee change records for "
          f"{len(result)} series from API.")
    return dict(result)


def taker_mult_at(series: str, candle_dt: datetime,
                  fee_changes: dict) -> float | None:
    """
    Return the taker fee multiplier for a given series at a given datetime.
    Returns None for zero-fee series.

    Priority:
      1. Zero-fee series list → None
      2. API fee change log → use the most recent change before candle_dt
      3. Hardcoded INX/NASDAQ100 fallback → INX_TAKER
      4. Default → STANDARD_TAKER
    """
    if series in ZERO_FEE_SERIES:
        return None

    if series in fee_changes:
        changes = fee_changes[series]
        effective_dates = [dt for dt, _ in changes]
        index = bisect_right(effective_dates, candle_dt) - 1
        if index >= 0:
            return changes[index][1]

    # Hardcoded fallbacks
    for prefix in INX_PREFIXES:
        if series.startswith(prefix):
            return INX_TAKER

    return STANDARD_TAKER


def candle_fee(candle: dict, series: str, candle_dt: datetime,
               fee_changes: dict, taker_fraction: float) -> float:
    """
    Compute Kalshi's total fee revenue from a single candlestick.

    Applies era-correct formula:
      Before MAKER_FEE_START: taker fee only (makers paid nothing)
      After  MAKER_FEE_START: taker fee + maker fee (Kalshi collects both)

    Uses explicit None check for price field to avoid falsy-zero fallthrough.
    """
    vol = float(candle.get("volume_fp") or candle.get("volume") or 0)
    if vol == 0:
        return 0.0

    price_block = candle.get("price") or {}
    p_raw = None
    for field in ("mean_dollars", "mean", "close_dollars", "close"):
        candidate = price_block.get(field)
        if candidate is not None:
            p_raw = candidate
            break
    if p_raw is None:
        return 0.0

    p = float(p_raw)
    if p <= 0.0 or p >= 1.0:
        return 0.0

    tm = taker_mult_at(series, candle_dt, fee_changes)
    if tm is None:
        return 0.0  # zero-fee series

    variance = p * (1.0 - p)
    taker_fee_per = math.ceil(tm * variance * 100) / 100

    if candle_dt < MAKER_FEE_START:
        # ERA 1: taker-only
        return taker_fee_per * vol
    else:
        # ERA 2: taker + maker, split by assumed fraction
        mm = tm * 0.25  # maker is always 25% of taker per fee schedule
        maker_fee_per = math.ceil(mm * variance * 100) / 100
        return (taker_fee_per * vol * taker_fraction +
                maker_fee_per * vol * (1.0 - taker_fraction))


# ── Candle period / intraday detection ───────────────────────────────────────

def is_intraday(open_time: str, close_time: str) -> bool:
    o = parse_iso(open_time)
    c = parse_iso(close_time)
    if not o or not c:
        return False
    return 0 < (c - o).total_seconds() / 3600 < INTRADAY_HOURS

def candle_period(open_time: str, close_time: str) -> int:
    return 1 if is_intraday(open_time, close_time) else 1440


# ── Candlestick fetchers ──────────────────────────────────────────────────────

def fetch_candles_live(series_ticker: str, market_ticker: str,
                       open_time: str = "", close_time: str = "") -> list:
    period = candle_period(open_time, close_time)
    path   = f"/series/{series_ticker}/markets/{market_ticker}/candlesticks"
    return get(path, {"period_interval": period}).get("candlesticks", [])

def fetch_candles_historical(market_ticker: str,
                              open_time: str = "", close_time: str = "") -> list:
    period = candle_period(open_time, close_time)
    path   = f"/historical/markets/{market_ticker}/candlesticks"
    params: dict = {"period_interval": period}
    o = parse_iso(open_time)
    c = parse_iso(close_time)
    if o and c:
        params["start_ts"] = int((o - timedelta(hours=6)).timestamp())
        params["end_ts"]   = int((c + timedelta(hours=6)).timestamp())
    return get(path, params).get("candlesticks", [])

def batch_fetch_candles_live(ticker_meta: dict[str, dict]) -> dict[str, list]:
    """
    Batch endpoint: up to 100 tickers per call. Split by period (1 vs 1440)
    because period_interval is a single parameter for the whole batch.
    """
    intraday = [t for t, m in ticker_meta.items()
                if is_intraday(m.get("open_time", ""), m.get("close_time", ""))]
    multiday = [t for t in ticker_meta if t not in intraday]
    result   = {}

    def _batch(tickers: list[str], period: int):
        for i in range(0, len(tickers), 100):
            chunk = tickers[i:i+100]
            data  = get("/markets/candlesticks",
                        {"tickers": ",".join(chunk), "period_interval": period})
            for entry in data.get("markets", []):
                tk = entry.get("market_ticker", "")
                cs = entry.get("candlesticks", [])
                if tk and cs:
                    result[tk] = cs
            time.sleep(DELAY)

    if intraday:
        print(f"       {len(intraday):,} intraday markets → 1-min candles", flush=True)
        _batch(intraday, 1)
    if multiday:
        print(f"       {len(multiday):,} multi-day markets → daily candles", flush=True)
        _batch(multiday, 1440)

    return result


# ── Category inference ────────────────────────────────────────────────────────

TICKER_CATEGORY = {
    "KXNBA":"sports",   "KXNFL":"sports",    "KXNHL":"sports",
    "KXMLB":"sports",   "KXNCAA":"sports",   "KXSOCCER":"sports",
    "KXWC":"sports",    "KXUFC":"sports",    "KXNASCAR":"sports",
    "KXPGA":"sports",   "KXBIG":"sports",    "KXCHAMP":"sports",
    "KXMLS":"sports",   "KXCFL":"sports",    "KXATP":"sports",
    "FED":"economics",  "KXFED":"economics", "KXCPI":"economics",
    "KXGDP":"economics","KXUNEMPLOYMENT":"economics","KXJOBS":"economics",
    "INX":"economics",  "NASDAQ100":"economics","KXEGGS":"economics",
    "KXAAAGASM":"economics",
    "KXBTC":"crypto",   "KXETH":"crypto",    "KXCRYPTO":"crypto",
    "KXPRES":"politics","KXSENATE":"politics","KXHOUSE":"politics",
    "KXGOV":"politics", "KXELECT":"politics",
    "KXHIGHNY":"weather","KXRAIN":"weather", "KXHURRICANE":"weather",
    "KXSNOW":"weather", "KXTEMP":"weather",
}

def categorize(ticker: str, api_category: str = "") -> str:
    if api_category and api_category.strip().lower() not in ("", "unknown"):
        return api_category.strip().lower()
    s = ticker.split("-")[0].upper()
    for prefix, cat in TICKER_CATEGORY.items():
        if s.startswith(prefix):
            return cat
    return "other"


# ── Core accumulator ──────────────────────────────────────────────────────────

def accumulate_candles(candles: list, series: str, category: str,
                       fee_changes: dict, taker_fraction: float,
                       daily_series: dict, min_date: str | None):
    """
    Process all candles for one market. For each candle:
      - Determine the calendar date from end_period_ts
      - Apply era-correct fee formula (taker-only before May 2025)
      - Accumulate into daily_series[date][category]

    min_date: if provided, skip candles before this date (for --days filtering)
    Returns (total_contracts, total_fee) for this market.
    """
    total_contracts = 0.0
    total_fee       = 0.0

    for c in candles:
        end_ts = c.get("end_period_ts")
        if end_ts is None:
            continue

        date_str = ts_to_date(int(end_ts))
        if min_date and date_str < min_date:
            continue

        candle_dt = datetime.fromtimestamp(int(end_ts), tz=timezone.utc)
        vol = float(c.get("volume_fp") or c.get("volume") or 0)

        fee = candle_fee(c, series, candle_dt, fee_changes, taker_fraction)

        if vol > 0:
            total_contracts += vol
            total_fee       += fee
            daily_series[date_str][category] += fee

    return total_contracts, total_fee


# ── Part A: Event Contracts ───────────────────────────────────────────────────

def process_event_contracts(fee_changes: dict, taker_fraction: float,
                             min_date: str | None) -> dict:
    print("\n══ PART A: EVENT CONTRACTS ══════════════════════════════════")

    # daily_series[date][category] = fee
    daily_series: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    total_markets   = 0
    total_contracts = 0.0
    total_fee       = 0.0
    no_candles      = 0

    # ── A1: Live markets ──────────────────────────────────────────────────────
    print("\n  [A1] Fetching live markets…")
    live_markets = {}
    for status in ("open", "active"):
        for m in paginate("/markets", "markets", {"status": status}):
            ticker = m.get("ticker")
            if ticker:
                live_markets[ticker] = m
    print(f"       {len(live_markets):,} live markets found")

    live_meta = {}
    for m in live_markets.values():
        live_meta[m["ticker"]] = {
            "series_ticker": m.get("series_ticker", ""),
            "category":      m.get("category", ""),
            "open_time":     m.get("open_time", ""),
            "close_time":    m.get("close_time", ""),
        }

    print("  [A1] Batch-fetching candlesticks…")
    candle_map = batch_fetch_candles_live(live_meta)

    for ticker, meta in live_meta.items():
        candles = candle_map.get(ticker, [])
        if not candles and meta["series_ticker"]:
            candles = fetch_candles_live(meta["series_ticker"], ticker,
                                         meta["open_time"], meta["close_time"])
            time.sleep(DELAY)

        if not candles:
            no_candles += 1
            continue

        series   = (meta.get("series_ticker") or ticker.split("-")[0]).upper()
        category = categorize(ticker, meta["category"])
        contracts, fee = accumulate_candles(
            candles, series, category, fee_changes, taker_fraction,
            daily_series, min_date
        )
        if contracts == 0:
            no_candles += 1
            continue

        total_markets   += 1
        total_contracts += contracts
        total_fee       += fee

        if total_markets % 500 == 0:
            print(f"       {total_markets:,} markets | fee ${total_fee:,.0f}", flush=True)

    print(f"  [A1] Done. {total_markets:,} live markets processed.")

    # ── A2: Historical markets ────────────────────────────────────────────────
    print("\n  [A2] Fetching historical markets…")
    hist_count = 0

    for m in paginate("/historical/markets", "markets"):
        ticker     = m.get("ticker", "")
        open_time  = m.get("open_time", "")
        close_time = m.get("close_time", "")
        api_cat    = m.get("category", "")

        # Skip entirely if the whole market closed before our min_date window
        if min_date and close_time and close_time[:10] < min_date:
            continue

        candles = fetch_candles_historical(ticker, open_time, close_time)
        time.sleep(DELAY)

        if not candles:
            no_candles += 1
            continue

        series   = (m.get("series_ticker") or ticker.split("-")[0]).upper()
        category = categorize(ticker, api_cat)
        contracts, fee = accumulate_candles(
            candles, series, category, fee_changes, taker_fraction,
            daily_series, min_date
        )
        if contracts == 0:
            no_candles += 1
            continue

        total_markets   += 1
        hist_count      += 1
        total_contracts += contracts
        total_fee       += fee

        if hist_count % 200 == 0:
            print(f"       {hist_count:,} historical | fee ${total_fee:,.0f}", flush=True)

    print(f"  [A2] Done. {hist_count:,} historical markets processed.")
    if no_candles:
        print(f"  Note: {no_candles:,} markets had no usable candle data.")

    return {
        "total_markets":   total_markets,
        "total_contracts": total_contracts,
        "total_fee":       total_fee,
        "daily_series":    {d: dict(cats) for d, cats in daily_series.items()},
    }


# ── Part B: Perpetual Futures ─────────────────────────────────────────────────

def process_perps(taker_fraction: float, min_date: str | None) -> dict:
    print("\n══ PART B: PERPETUAL FUTURES ════════════════════════════════")
    print("   Launched May 29, 2026 — only ~1 month of history available")

    perp_markets = list(paginate("/margin/markets", "markets"))
    if not perp_markets:
        print("   No perp markets returned.")
        return {"total_fee": 0.0, "total_notional": 0.0,
                "daily_series": {}, "note": "no data",
                "all_time_adjustment": False}

    print(f"   {len(perp_markets):,} perp markets found")
    total_notional = 0.0
    total_fee      = 0.0

    for m in perp_markets:
        ticker = m.get("ticker", "")

        # Prefer explicit notional volume field; explicit None check
        notional = None
        for field in ("volume_notional_dollars", "lifetime_volume_notional_dollars"):
            v = m.get(field)
            if v is not None:
                notional = float(v)
                break
        if notional is None:
            contracts = float(m.get("volume") or m.get("volume_fp") or 0)
            price     = float(m.get("mark_price") or m.get("last_price") or 0)
            notional  = contracts * price

        blended = (PERP_TAKER_BPS * taker_fraction +
                   PERP_MAKER_BPS * (1 - taker_fraction))
        fee = notional * blended

        total_notional += notional
        total_fee      += fee

    note = (f"defaults: {PERP_MAKER_BPS*1e4:.1f} bps maker / "
            f"{PERP_TAKER_BPS*1e4:.1f} bps taker / "
            f"{taker_fraction*100:.0f}% taker assumed")
    print(f"   Total notional: ${total_notional:,.0f} | Fee: ${total_fee:,.0f}")
    print(f"   ({note})")

    return {"total_fee": total_fee, "total_notional": total_notional,
            "daily_series": {}, "note": note, "all_time_adjustment": True}


# ── Output ────────────────────────────────────────────────────────────────────

def print_and_save(event: dict, perps: dict, taker_fraction: float, output_dir: str):
    ef = event["total_fee"]
    pf = perps.get("total_fee", 0.0)
    gt = ef + pf

    # Merge daily series: event by category + perps as "crypto_perps"
    all_dates: set[str] = set(event["daily_series"].keys())
    all_dates.update(perps.get("daily_series", {}).keys())
    all_dates_sorted = sorted(all_dates)

    # ── Daily table (console) ─────────────────────────────────────────────────
    print("\n" + "═"*72)
    print("  DAILY FEE ESTIMATE")
    print("═"*72)
    print(f"  {'Date':<12} {'Daily Fee':>14} {'Cumulative':>14}  Category Breakdown")
    print("  " + "─"*68)

    cumulative = 0.0
    daily_rows = []

    for date in all_dates_sorted:
        day_cats = event["daily_series"].get(date, {})
        perp_fee = perps.get("daily_series", {}).get(date, 0.0)
        day_total = sum(day_cats.values()) + perp_fee
        cumulative += day_total

        # Top 2 categories for console display
        top = sorted(day_cats.items(), key=lambda x: x[1], reverse=True)[:2]
        top_str = ", ".join(f"{c}: ${v:,.0f}" for c, v in top)
        if perp_fee > 0:
            top_str = (top_str + f", perps: ${perp_fee:,.0f}").lstrip(", ")

        print(f"  {date:<12} ${day_total:>13,.2f} ${cumulative:>13,.2f}  {top_str}")

        daily_rows.append({
            "date": date,
            "daily_fee": round(day_total, 2),
            "cumulative_fee": round(cumulative, 2),
            **{f"cat_{k}": round(v, 2) for k, v in day_cats.items()},
            "cat_perps": round(perp_fee, 2),
        })

    print("  " + "─"*68)
    print(f"  {'TOTAL':<12} ${gt:>13,.2f}")

    # ── Monthly summary ───────────────────────────────────────────────────────
    monthly: dict[str, float] = defaultdict(float)
    for row in daily_rows:
        monthly[date_to_month(row["date"])] += row["daily_fee"]

    print("\n" + "═"*40)
    print("  MONTHLY SUMMARY")
    print("═"*40)
    print(f"  {'Month':<10} {'Fee':>14}  {'vs Prior Mo':>12}")
    months_sorted = sorted(monthly.keys())
    prior = None
    monthly_rows = []
    for mo in months_sorted:
        fee = monthly[mo]
        if prior is not None:
            pct = (fee - prior) / prior * 100 if prior else 0
            chg = f"{pct:+.1f}%"
        else:
            chg = "—"
        print(f"  {mo:<10} ${fee:>13,.2f}  {chg:>12}")
        monthly_rows.append({"month": mo, "fee": round(fee, 2)})
        prior = fee

    # ── Totals and run rate ───────────────────────────────────────────────────
    print("\n" + "═"*52)
    print("  TOTALS & RUN RATE")
    print("═"*52)
    print(f"  All-time event fee:       ${ef:>15,.2f}")
    print(f"  Perp fee:                 ${pf:>15,.2f}")
    if perps.get("all_time_adjustment"):
        print("  Perp daily series:        excluded (no daily perp history available)")
    print(f"  Grand total:              ${gt:>15,.2f}")
    print()

    # Trailing 30-day annualized (current run rate, not diluted by early years)
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff30 = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    recent30 = sum(r["daily_fee"] for r in daily_rows if r["date"] >= cutoff30)
    if recent30 > 0:
        annual30 = recent30 * (365 / 30)
        print(f"  Trailing 30d fee:         ${recent30/1e6:>12.2f}M")
        print(f"  Trailing 30d annualized:  ${annual30/1e6:>12.1f}M/year  ← current run rate")
    print()

    # Fee era split
    era1_dates = [r for r in daily_rows if r["date"] < "2025-05-01"]
    era2_dates = [r for r in daily_rows if r["date"] >= "2025-05-01"]
    era1_total = sum(r["daily_fee"] for r in era1_dates)
    era2_total = sum(r["daily_fee"] for r in era2_dates)
    if era1_total > 0 or era2_total > 0:
        print(f"  Era 1 (taker-only, pre-May 2025): ${era1_total/1e6:.2f}M")
        print(f"  Era 2 (taker+maker, May 2025+):   ${era2_total/1e6:.2f}M")
        print()

    # ── Sensitivity ──────────────────────────────────────────────────────────
    base_tf   = taker_fraction
    base_rate = STANDARD_TAKER * base_tf + STANDARD_MAKER * (1 - base_tf)
    print("  Maker/taker sensitivity (approximate; standard current fee rates only):")
    for tf in (0.60, 0.70, 0.80):
        rate  = STANDARD_TAKER * tf + STANDARD_MAKER * (1 - tf)
        scaled = gt * (rate / base_rate) if base_rate > 0 else gt
        marker = " ← base" if abs(tf - base_tf) < 0.01 else ""
        print(f"    {tf*100:.0f}/{(1-tf)*100:.0f}: ${scaled/1e6:.1f}M{marker}")
    print()

    # ── Methodology ──────────────────────────────────────────────────────────
    print("  METHODOLOGY & APPROXIMATIONS:")
    print(f"  - Era 1 (pre-May 2025): taker-only formula (0.07 × P × (1-P))")
    print(f"  - Era 2 (May 2025+): taker + maker, split {taker_fraction*100:.0f}%/{(1-taker_fraction)*100:.0f}%")
    print("  - Fee era boundary applied per candle, not per market")
    print("  - Per-series multipliers from GET /exchange/series-fee-changes")
    print("  - Intraday markets (<24h): 1-min candles; multi-day: daily candles")
    print("  - VWAP approx: time-weighted midpoint mean, not fill-price VWAP")
    print("  - ceil() rounding per-market (vs per-fill — slight undercount)")
    print("  - Zero-fee series excluded: KXBTCY, KXCITRINI, KXDOED")
    print("  - Perp fees: bps defaults used; authenticated actual rates are not queried")
    print("  - Funding rate excluded (trader-to-trader, not to Kalshi)")
    print("═"*52)

    # ── CSV outputs ───────────────────────────────────────────────────────────
    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    daily_path   = output_path / "kalshi_fee_daily.csv"
    monthly_path = output_path / "kalshi_fee_monthly.csv"

    # Daily CSV — dynamic category columns
    all_cat_keys = set()
    for r in daily_rows:
        all_cat_keys.update(k for k in r if k.startswith("cat_"))
    cat_cols = sorted(all_cat_keys)
    daily_fields = ["date", "daily_fee", "cumulative_fee"] + cat_cols
    with open(daily_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=daily_fields, extrasaction="ignore")
        w.writeheader()
        for row in daily_rows:
            w.writerow({k: row.get(k, 0) for k in daily_fields})

    with open(monthly_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["month", "fee"])
        w.writeheader()
        w.writerows(monthly_rows)

    print(f"\n  Saved: {daily_path}")
    print(f"  Saved: {monthly_path}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Kalshi fee revenue estimator — daily time series with era-correct fee formulas"
    )
    parser.add_argument("--taker-fraction", type=float, default=DEFAULT_TAKER_FRACTION,
                        help=f"Fraction of volume that is taker-initiated (default: {DEFAULT_TAKER_FRACTION})")
    parser.add_argument("--days", type=int, default=None,
                        help="Only include data from the last N days (default: all time)")
    parser.add_argument("--skip-perps",       action="store_true")
    parser.add_argument("--output-dir", default=".",
                        help="Directory for kalshi_fee_daily.csv and kalshi_fee_monthly.csv (default: current directory)")
    args = parser.parse_args()

    min_date = None
    if args.days:
        min_date = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")

    print("Kalshi Fee Revenue Estimator v4")
    print(f"Base URL:       {BASE_URL}")
    print(f"Taker fraction: {args.taker_fraction*100:.0f}%")
    print(f"Window:         {'all time' if not min_date else f'from {min_date}'}")
    print(f"Fee era:        taker-only before May 2025 / taker+maker after")
    print()

    print("  [0] Loading fee change history…")
    fee_changes = load_fee_changes()

    event_results = process_event_contracts(fee_changes, args.taker_fraction, min_date)

    perp_results = {"total_fee": 0.0, "total_notional": 0.0,
                    "daily_series": {}, "note": "skipped",
                    "all_time_adjustment": False}
    if not args.skip_perps:
        perp_results = process_perps(args.taker_fraction, min_date)

    print_and_save(event_results, perp_results, args.taker_fraction, args.output_dir)


if __name__ == "__main__":
    main()
