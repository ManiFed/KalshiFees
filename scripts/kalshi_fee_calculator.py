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

  ERA 1: Launch → per-series maker-fee rollout (mostly pre-Oct 2025)
    fee_type = quadratic → takers only.
    Formula: ceil(0.07 × mult × P × (1-P), ¢) × contracts

  ERA 2: Per-series quadratic_with_maker_fees (sports from Oct 2025, rolling)
    Both taker AND maker pay on every fill (GWU 2026-001; Kalshi fee schedule).
    Revenue per trade = taker_fee + maker_fee (no volume-split assumption).

  Per-series schedule:
    GET /series/fee_changes?show_historical=true → multiplier scale + fee_type
    GET /series/{ticker} → fallback multiplier (e.g. 0.5 for INX daily, 0 for KXBTCY)

────────────────────────────────────────────────────────────────────────
ARCHITECTURE
────────────────────────────────────────────────────────────────────────

Step 1 — Load fee change history
  GET /series/fee_changes?show_historical=true → per-series multiplier + fee_type
  GET /series/{ticker} → fallback multiplier for series absent from the change log

Step 2 — Enumerate all markets
  Live:       GET /markets (status=open, mve_filter=exclude, activity filter)
  Historical: GET /historical/markets

Step 3 — Fetch candlestick data per market
  Live batch: GET /markets/candlesticks (market_tickers + start_ts + end_ts)
  Historical: GET /historical/markets/{ticker}/candlesticks
  Intraday (<24h) → 1-min candles; otherwise daily

Step 4 — Compute fee per candle, accumulate daily time series
  Price: candle mean_dollars (mean traded YES price); fee from contract_fees()
  fee_type=quadratic → taker only; quadratic_with_maker_fees → taker + maker
  Per-series schedule applied by candle end_period_ts (not a global calendar cut)

Step 5 — Perpetual futures (separate /margin/ API rail)
  Launched May 29, 2026. Revenue = maker/taker bps on notional volume.
  Funding rate flows between traders, not to Kalshi — excluded.

Step 6 — Output
  • Daily time series table (date, daily_fee, cumulative_fee, category breakdown)
  • Monthly summary table
  • All-time total and trailing-30d annualized run rate
  • Sensitivity table for fee multiplier uncertainty
  • CSV: kalshi_fee_daily.csv and kalshi_fee_monthly.csv

────────────────────────────────────────────────────────────────────────
KNOWN APPROXIMATIONS
────────────────────────────────────────────────────────────────────────
  1. Candle mode uses mean_dollars (mean traded price); trade mode uses fill price
     Direction: small residual error when mean_dollars unavailable (no-trade candles)
  2. ceil() rounding applied per-candle bucket, not per-fill
     Direction: slight undercount vs per-fill rounding on small trades
  3. Series without fee_changes entries default to taker-only before Oct 2025
  4. Zero-fee series list may be incomplete if API omits them
     Direction: slight overcount if any zero-fee series are missing
  6. Perp fee bps are defaults; actual authenticated rates are not queried
  7. 6h candle buffer may still miss pre-open activity on some markets

────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────
  pip install requests
  python kalshi_fee_calculator.py
  python kalshi_fee_calculator.py --days 90          # limit to last 90 days
  python kalshi_fee_calculator.py --skip-live        # historical markets only (bootstrap)
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

# ── Fee constants (Kalshi fee schedule: 0.07 × C × P × (1-P), maker = 25% of taker) ─
STANDARD_TAKER = 0.07
STANDARD_MAKER = 0.0175

# Sports maker-fee rollout began Oct–Nov 2025 per GET /series/fee_changes; use API timeline.
PRE_MAKER_FEES_CUTOFF = datetime(2025, 10, 1, tzinfo=timezone.utc)

# Zero-fee series (fee_multiplier=0 in GET /series/{ticker}); kept as fallback.
ZERO_FEE_SERIES = frozenset({"KXBTCY", "KXCITRINI", "KXDOED"})

_series_fee_cache: dict[str, tuple[float, str]] = {}

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
                print(f"    [error] {path}: {e}", flush=True)
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


# ── Fee schedule (GET /series/fee_changes + GET /series/{ticker}) ─────────────

def load_fee_changes() -> dict[str, list[tuple[datetime, float, str]]]:
    """
    Fetch GET /series/fee_changes?show_historical=true.

    Returns {series_ticker: [(effective_dt, fee_multiplier_scale, fee_type), ...]}
    sorted ascending. fee_multiplier is a scale on the standard 0.07 / 0.0175 rates
    (e.g. 0.5 → half fees for INX/Nasdaq daily markets; 0 → zero-fee).
    """
    data = get("/series/fee_changes", {"show_historical": True})
    changes_raw = data.get("series_fee_change_arr", [])

    result: dict[str, list[tuple[datetime, float, str]]] = defaultdict(list)
    for c in changes_raw:
        series   = c.get("series_ticker", "")
        mult_raw = c.get("fee_multiplier")
        ftype    = c.get("fee_type", "quadratic")
        ts_str   = c.get("scheduled_ts", "")
        if not series or mult_raw is None or not ts_str:
            continue
        dt = parse_iso(ts_str)
        if dt:
            result[series].append((dt, float(mult_raw), ftype))

    for series in result:
        result[series].sort(key=lambda x: x[0])

    print(f"  Loaded {len(changes_raw)} fee change records for "
          f"{len(result)} series from API.")
    return dict(result)


def get_series_fee_default(series: str) -> tuple[float, str]:
    if series in _series_fee_cache:
        return _series_fee_cache[series]
    data = get(f"/series/{series}")
    meta = data.get("series", data) if isinstance(data, dict) else {}
    mult = float(meta.get("fee_multiplier", 1))
    ftype = meta.get("fee_type", "quadratic")
    _series_fee_cache[series] = (mult, ftype)
    return mult, ftype


def fee_state_at(series: str, dt: datetime,
                 fee_changes: dict) -> tuple[float, str]:
    """
    Return (fee_multiplier_scale, fee_type) for a series at datetime dt.

    fee_type values from Kalshi API:
      quadratic                  → taker fees only
      quadratic_with_maker_fees  → taker + maker fees (both sides pay per fill)
      margin_market_maker_program_fees → perp MM program (treated as zero here)
    """
    if series in ZERO_FEE_SERIES:
        return 0.0, "quadratic"

    changes = fee_changes.get(series, [])
    if changes:
        dates = [row[0] for row in changes]
        idx = bisect_right(dates, dt) - 1
        if idx >= 0:
            return changes[idx][1], changes[idx][2]
        return changes[0][1], "quadratic"

    mult, ftype = get_series_fee_default(series)
    if dt < PRE_MAKER_FEES_CUTOFF:
        return mult, "quadratic"
    return mult, ftype


def contract_fees(count: float, price: float, series: str, dt: datetime,
                  fee_changes: dict) -> float:
    """
    Kalshi exchange revenue for count contracts at price P.

    Each matched trade collects taker fee from the taker AND maker fee from the
    maker when fee_type is quadratic_with_maker_fees — no volume-split assumption.
    """
    if count <= 0 or price <= 0.0 or price >= 1.0:
        return 0.0

    mult, ftype = fee_state_at(series, dt, fee_changes)
    if mult == 0 or ftype == "margin_market_maker_program_fees":
        return 0.0

    variance = price * (1.0 - price)
    taker_per = math.ceil(STANDARD_TAKER * mult * variance * 100) / 100
    total = taker_per * count

    if ftype == "quadratic_with_maker_fees":
        maker_per = math.ceil(STANDARD_MAKER * mult * variance * 100) / 100
        total += maker_per * count

    return total


def candle_trade_price(candle: dict) -> float | None:
    """
    Use Kalshi's mean_dollars: documented as the mean traded YES price in the
    candle period (not bid/ask midpoint). Returns None when no trades occurred.
    """
    price_block = candle.get("price") or {}
    raw = price_block.get("mean_dollars")
    if raw is None:
        return None
    p = float(raw)
    if p <= 0.0 or p >= 1.0:
        return None
    return p


def trade_fee(trade: dict, series: str, fee_changes: dict) -> float:
    """Exact fill-price fee from GET /markets/trades (yes_price_dollars + count_fp)."""
    count = float(trade.get("count_fp") or trade.get("count") or 0)
    price = float(trade.get("yes_price_dollars") or 0)
    created = parse_iso(trade.get("created_time", ""))
    if not created or count <= 0:
        return 0.0
    return contract_fees(count, price, series, created, fee_changes)


def candle_fee(candle: dict, series: str, candle_dt: datetime,
               fee_changes: dict) -> float:
    """Fee estimate for one candlestick bucket using mean traded price."""
    vol = float(candle.get("volume_fp") or candle.get("volume") or 0)
    if vol == 0:
        return 0.0
    price = candle_trade_price(candle)
    if price is None:
        return 0.0
    return contract_fees(vol, price, series, candle_dt, fee_changes)


# ── Candle period / intraday detection ───────────────────────────────────────

def is_intraday(open_time: str, close_time: str) -> bool:
    o = parse_iso(open_time)
    c = parse_iso(close_time)
    if not o or not c:
        return False
    return 0 < (c - o).total_seconds() / 3600 < INTRADAY_HOURS

def candle_period(open_time: str, close_time: str) -> int:
    return 1 if is_intraday(open_time, close_time) else 1440

def candle_time_params(open_time: str = "", close_time: str = "") -> dict[str, int]:
    now = datetime.now(timezone.utc)
    o = parse_iso(open_time)
    c = parse_iso(close_time)
    start = int((o - timedelta(hours=6)).timestamp()) if o else int((now - timedelta(days=90)).timestamp())
    end   = int((c + timedelta(hours=6)).timestamp()) if c else int(now.timestamp())
    if end <= start:
        end = start + 3600
    return {"start_ts": start, "end_ts": end}

def market_has_activity(market: dict) -> bool:
    for field in ("volume_fp", "volume_24h_fp", "open_interest_fp", "volume", "volume_24h"):
        val = market.get(field)
        if val is not None and float(val) > 0:
            return True
    return False


# ── Candlestick fetchers ──────────────────────────────────────────────────────

def fetch_candles_live(series_ticker: str, market_ticker: str,
                       open_time: str = "", close_time: str = "") -> list:
    period = candle_period(open_time, close_time)
    path   = f"/series/{series_ticker}/markets/{market_ticker}/candlesticks"
    params = {"period_interval": period, **candle_time_params(open_time, close_time)}
    return get(path, params).get("candlesticks", [])

def fetch_candles_historical(market_ticker: str,
                              open_time: str = "", close_time: str = "") -> list:
    period = candle_period(open_time, close_time)
    path   = f"/historical/markets/{market_ticker}/candlesticks"
    params = {"period_interval": period, **candle_time_params(open_time, close_time)}
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
            starts, ends = [], []
            for ticker in chunk:
                meta = ticker_meta.get(ticker, {})
                bounds = candle_time_params(meta.get("open_time", ""), meta.get("close_time", ""))
                starts.append(bounds["start_ts"])
                ends.append(bounds["end_ts"])
            data = get(
                "/markets/candlesticks",
                {
                    "market_tickers": ",".join(chunk),
                    "period_interval": period,
                    "start_ts": min(starts),
                    "end_ts": max(ends),
                },
            )
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
                       fee_changes: dict, daily_series: dict,
                       min_date: str | None):
    """
    Process all candles for one market. For each candle:
      - Determine the calendar date from end_period_ts
      - Apply per-series fee_type + multiplier from fee change log
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

        fee = candle_fee(c, series, candle_dt, fee_changes)

        if vol > 0:
            total_contracts += vol
            total_fee       += fee
            daily_series[date_str][category] += fee

    return total_contracts, total_fee


# ── Part A: Event Contracts ───────────────────────────────────────────────────

def process_event_contracts(fee_changes: dict, min_date: str | None,
                             skip_live: bool = False) -> dict:
    print("\n══ PART A: EVENT CONTRACTS ══════════════════════════════════")

    # daily_series[date][category] = fee
    daily_series: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    total_markets   = 0
    total_contracts = 0.0
    total_fee       = 0.0
    no_candles      = 0

    # ── A1: Live markets ──────────────────────────────────────────────────────
    if skip_live:
        print("\n  [A1] Skipping live markets (--skip-live).")
    else:
        print("\n  [A1] Fetching live markets with activity…")
        live_markets = {}
        scanned = 0
        for m in paginate("/markets", "markets", {"status": "open", "mve_filter": "exclude"}):
            scanned += 1
            ticker = m.get("ticker")
            if ticker and market_has_activity(m):
                live_markets[ticker] = m
        print(f"       {len(live_markets):,} active open markets (scanned {scanned:,})")

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

        live_processed = 0
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
                candles, series, category, fee_changes, daily_series, min_date
            )
            if contracts == 0:
                no_candles += 1
                continue

            total_markets   += 1
            live_processed  += 1
            total_contracts += contracts
            total_fee       += fee

            if live_processed % 500 == 0:
                print(f"       {live_processed:,} live markets | fee ${total_fee:,.0f}", flush=True)

        print(f"  [A1] Done. {live_processed:,} live markets processed.")

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
            candles, series, category, fee_changes, daily_series, min_date
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

def process_perps(min_date: str | None) -> dict:
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

        fee = notional * (PERP_TAKER_BPS + PERP_MAKER_BPS)

        total_notional += notional
        total_fee      += fee

    note = (f"defaults: {PERP_MAKER_BPS*1e4:.1f} bps maker + "
            f"{PERP_TAKER_BPS*1e4:.1f} bps taker on notional")
    print(f"   Total notional: ${total_notional:,.0f} | Fee: ${total_fee:,.0f}")
    print(f"   ({note})")

    return {"total_fee": total_fee, "total_notional": total_notional,
            "daily_series": {}, "note": note, "all_time_adjustment": True}


# ── Output ────────────────────────────────────────────────────────────────────

def print_and_save(event: dict, perps: dict, output_dir: str):
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

    # Fee era split (approximate calendar cut — actual maker fees are per-series)
    era1_dates = [r for r in daily_rows if r["date"] < "2025-10-01"]
    era2_dates = [r for r in daily_rows if r["date"] >= "2025-10-01"]
    era1_total = sum(r["daily_fee"] for r in era1_dates)
    era2_total = sum(r["daily_fee"] for r in era2_dates)
    if era1_total > 0 or era2_total > 0:
        print(f"  Pre-Oct 2025 fees (mostly taker-only):  ${era1_total/1e6:.2f}M")
        print(f"  Oct 2025+ fees (incl. maker rollouts):  ${era2_total/1e6:.2f}M")
        print()

    # ── Sensitivity ──────────────────────────────────────────────────────────
    print("  Fee multiplier sensitivity (standard 0.07/0.0175 quadratic rates):")
    for mult in (0.5, 1.0, 1.25):
        scaled = gt * mult
        marker = " ← base" if abs(mult - 1.0) < 0.01 else ""
        print(f"    mult {mult:.2f}×: ${scaled/1e6:.1f}M{marker}")
    print()

    # ── Methodology ──────────────────────────────────────────────────────────
    print("  METHODOLOGY & APPROXIMATIONS:")
    print("  - fee_type from GET /series/fee_changes?show_historical=true")
    print("  - quadratic → taker only; quadratic_with_maker_fees → taker + maker per fill")
    print("  - Per-series multiplier scale from API (0.5× INX/Nasdaq, 0× zero-fee)")
    print("  - Candle price: mean_dollars (mean traded YES price in period)")
    print("  - Trade-level alternative: GET /markets/trades (yes_price_dollars per fill)")
    print("  - Intraday markets (<24h): 1-min candles; multi-day: daily candles")
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
    parser.add_argument("--days", type=int, default=None,
                        help="Only include data from the last N days (default: all time)")
    parser.add_argument("--skip-perps",       action="store_true")
    parser.add_argument("--skip-live",        action="store_true",
                        help="Skip open-market scan; use historical markets only")
    parser.add_argument("--output-dir", default=".",
                        help="Directory for kalshi_fee_daily.csv and kalshi_fee_monthly.csv (default: current directory)")
    args = parser.parse_args()

    min_date = None
    if args.days:
        min_date = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")

    print("Kalshi Fee Revenue Estimator v4")
    print(f"Base URL:       {BASE_URL}")
    print(f"Window:         {'all time' if not min_date else f'from {min_date}'}")
    print(f"Fee schedule:   GET /series/fee_changes + per-fill taker+maker when applicable")
    print(f"Fee era:        per-series fee_type from API (sports maker fees from Oct 2025)")
    print()

    print("  [0] Loading fee change history…")
    fee_changes = load_fee_changes()

    event_results = process_event_contracts(
        fee_changes, min_date, skip_live=args.skip_live
    )

    perp_results = {"total_fee": 0.0, "total_notional": 0.0,
                    "daily_series": {}, "note": "skipped",
                    "all_time_adjustment": False}
    if not args.skip_perps:
        perp_results = process_perps(min_date)

    print_and_save(event_results, perp_results, args.output_dir)


if __name__ == "__main__":
    main()
