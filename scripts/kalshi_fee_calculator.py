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
  Live:       GET /markets (status=open) twice — mve_filter=exclude then only
  Historical: GET /historical/markets

Step 3 — Fetch candlestick data per market
  Live batch: GET /markets/candlesticks (market_tickers + start_ts + end_ts)
  Historical: GET /historical/markets/{ticker}/candlesticks
  Period: 1-min for sports / high-volume / short-lived (chunked ≤10k candles);
          daily otherwise. If recovered volume ≪ listing volume → re-fetch 1-min,
          then fall back to GET /markets/trades pagination for exact fill fees.

Step 4 — Compute fee per candle (or trade), accumulate daily time series
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
  1. Candle mode uses mean/mean_dollars (mean traded price); trade mode uses fill price
     Direction: small residual error when no trade price fields exist on a candle
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
  python kalshi_fee_calculator.py --checkpoint data/scan.json --resume  # VPS full scan
  python kalshi_fee_calculator.py --checkpoint data/scan.json --resume --fail-on-incomplete
"""

import math
import csv
import gc
import json
import os
import time
import argparse
import tempfile
import requests
from bisect import bisect_right
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DELAY    = 0.12
MAX_PAGES = 2000
REQUEST_TIMEOUT = 45
CANDLE_TIMEOUT = 90
MAX_RETRIES = 5
CHECKPOINT_VERSION = 2  # v2: 1-min sports/high-vol, trade fallback, MVE live pass
CHECKPOINT_EVERY = 200
CANDLE_BATCH_SIZE = 25
LIVE_PROCESS_CHUNK = 200
MIN_EXPECTED_MARKETS = 150_000
MIN_EXPECTED_DATE_SPAN = 30
MAX_CANDLES_PER_REQUEST = 10_000
HIGH_VOLUME_ONE_MIN = 5_000.0
VOLUME_RECOVERY_RATIO = 0.5
VOLUME_RECOVERY_MIN_LISTING = 500.0
TRADE_FALLBACK_MAX_PAGES = 500

# ── Fee constants (Kalshi fee schedule: 0.07 × C × P × (1-P), maker = 25% of taker) ─
STANDARD_TAKER = 0.07
STANDARD_MAKER = 0.0175

# Sports maker-fee rollout began Oct–Nov 2025 per GET /series/fee_changes; use API timeline.
PRE_MAKER_FEES_CUTOFF = datetime(2025, 10, 1, tzinfo=timezone.utc)

# Zero-fee series (fee_multiplier=0 in GET /series/{ticker}); kept as fallback.
ZERO_FEE_SERIES = frozenset({"KXBTCY", "KXCITRINI", "KXDOED"})

MVE_SPORTS_MAKER_PREFIXES = (
    "KXMVESPORTSMULTIGAMEEXTENDED",
    "KXMVESPORTSMULTIGAME",
    "KXMVECROSSCATEGORY",
)

_series_fee_cache: dict[str, tuple[float, str]] = {}

INTRADAY_HOURS = 24

# Perp fee defaults
PERP_TAKER_BPS = 0.00050
PERP_MAKER_BPS = 0.00020


# ── HTTP helpers ──────────────────────────────────────────────────────────────

SESSION = requests.Session()


def _format_request_error(path: str, params: dict | None, exc: Exception) -> str:
    if not params:
        return f"{path}: {exc}"
    short = dict(params)
    tickers = short.get("market_tickers")
    if isinstance(tickers, str) and "," in tickers:
        parts = tickers.split(",")
        short["market_tickers"] = f"{len(parts)} tickers ({parts[0]}…)"
    elif isinstance(tickers, str) and tickers:
        short["market_tickers"] = tickers[:40] + ("…" if len(tickers) > 40 else "")
    return f"{path} {short}: {exc}"


def get_once(path: str, params: dict = None,
             timeout: int = REQUEST_TIMEOUT) -> tuple[dict | None, int | None]:
    """Single GET returning (json_body, status_code). status_code is None on transport failure."""
    url = BASE_URL + path
    try:
        r = SESSION.get(url, params=params, timeout=timeout)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 10))
            print(f"    [rate limit] sleeping {wait}s…", flush=True)
            time.sleep(wait)
            return None, 429
        if r.status_code >= 400:
            return None, r.status_code
        return r.json(), r.status_code
    except requests.RequestException as e:
        print(f"    [error] {_format_request_error(path, params, e)}", flush=True)
        return None, None


def get(path: str, params: dict = None, retries: int = MAX_RETRIES,
        timeout: int = REQUEST_TIMEOUT) -> dict | None:
    for attempt in range(retries):
        data, status = get_once(path, params, timeout=timeout)
        if status == 429:
            continue
        if status is not None and status >= 400:
            if attempt == retries - 1:
                print(
                    f"    [error] {_format_request_error(path, params, requests.HTTPError(f'{status}'))}",
                    flush=True,
                )
                return None
            time.sleep(min(60, 2 ** attempt * 3))
            continue
        if data is not None:
            return data
        if attempt == retries - 1:
            return None
        time.sleep(min(60, 2 ** attempt * 3))
    return None


def fetch_page(path: str, result_key: str, params: dict | None = None,
               cursor: str = "") -> dict | None:
    """Fetch one paginated API page; None on hard failure (caller should checkpoint)."""
    page_params = dict(params or {})
    page_params.setdefault("limit", 1000)
    if cursor:
        page_params["cursor"] = cursor
    data = get(path, page_params)
    if data is None or result_key not in data:
        return None
    return data


def paginate(path: str, result_key: str, params: dict = None,
             start_cursor: str = ""):
    params = dict(params or {})
    params.setdefault("limit", 1000)
    cursor = start_cursor
    pages = 0
    total = 0
    while pages < MAX_PAGES:
        data = fetch_page(path, result_key, params, cursor)
        if data is None:
            print(f"\n  ⚠ WARNING: pagination failed for {path} at cursor "
                  f"({total} items retrieved, results INCOMPLETE)\n", flush=True)
            break
        items = data.get(result_key, [])
        if not items:
            break
        yield from items
        total += len(items)
        cursor = data.get("cursor", "")
        pages += 1
        if not cursor:
            break
        time.sleep(DELAY)
    if pages >= MAX_PAGES:
        print(f"\n  ⚠ WARNING: pagination ceiling hit for {path} "
              f"({total} items retrieved, results INCOMPLETE)\n", flush=True)


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

def normalize_ts(raw) -> int | None:
    """Normalize candle end_period_ts (seconds or milliseconds) to Unix seconds."""
    if raw is None:
        return None
    ts = int(raw)
    if ts > 1_000_000_000_000:
        ts //= 1000
    return ts


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
    data = get("/series/fee_changes", {"show_historical": True}) or {}
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
    data = get(f"/series/{series}", retries=3, timeout=20) or {}
    meta = data.get("series", data) if isinstance(data, dict) else {}
    if not meta:
        mult, ftype = 1.0, "quadratic"
    else:
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
    if ftype == "quadratic" and any(series.startswith(p) for p in MVE_SPORTS_MAKER_PREFIXES):
        return mult, "quadratic_with_maker_fees"
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


def normalize_contract_price(raw) -> float | None:
    """Parse YES contract price from API (dollars 0–1, or legacy 1–99 cent quotes)."""
    if raw is None:
        return None
    p = float(raw)
    if p <= 0.0:
        return None
    if p > 1.0:
        if p <= 100.0:
            p /= 100.0
        else:
            return None
    if p >= 1.0:
        return None
    return p


def candle_trade_price(candle: dict) -> float | None:
    """
    Mean traded YES price for the candle period.
    Prefers mean_dollars (current API); falls back to legacy mean/close fields.
    """
    price_block = candle.get("price") or {}
    for field in ("mean_dollars", "mean", "close_dollars", "close"):
        p = normalize_contract_price(price_block.get(field))
        if p is not None:
            return p
    return None


def listing_volume(market: dict) -> float:
    for field in ("volume_fp", "volume"):
        val = market.get(field)
        if val is not None and float(val) > 0:
            return float(val)
    return 0.0


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

def series_ticker_of(market: dict | str, series_ticker: str = "") -> str:
    """
    Prefer API series_ticker; MVE/combo markets often omit it — derive from ticker prefix.
    """
    if isinstance(market, dict):
        ticker = market.get("ticker", "") or ""
        series = (market.get("series_ticker") or series_ticker or "").strip()
    else:
        ticker = market or ""
        series = (series_ticker or "").strip()
    if series:
        return series.upper()
    return (ticker.split("-")[0] if ticker else "").upper()


def market_duration_hours(open_time: str, close_time: str) -> float | None:
    o = parse_iso(open_time)
    c = parse_iso(close_time)
    if not o or not c:
        return None
    return (c - o).total_seconds() / 3600.0


def is_intraday(open_time: str, close_time: str) -> bool:
    hours = market_duration_hours(open_time, close_time)
    return hours is not None and 0 < hours < INTRADAY_HOURS


def choose_candle_period(open_time: str, close_time: str,
                         category: str = "", listing_vol: float = 0.0,
                         ticker: str = "") -> int:
    """
    Daily (1440) candles systematically under-recover volume on multi-day sports
    and other high-activity markets (e.g. ~965 contracts on daily vs ~1.8M on 1-min).
    Prefer 1-min whenever under-recovery is likely; chunk long ranges below.
    """
    hours = market_duration_hours(open_time, close_time)
    if hours is not None and 0 < hours < INTRADAY_HOURS:
        return 1
    cat = categorize(ticker, category) if (ticker or category) else (category or "").lower()
    if cat == "sports":
        return 1
    series = series_ticker_of(ticker)
    if any(series.startswith(p) for p in MVE_SPORTS_MAKER_PREFIXES):
        return 1
    if listing_vol >= HIGH_VOLUME_ONE_MIN:
        return 1
    # Multi-day but not huge: still prefer 1-min when listing volume is material
    if hours is not None and hours < 24 * 14 and listing_vol >= 1_000:
        return 1
    return 1440


def candle_period(open_time: str, close_time: str,
                  category: str = "", listing_vol: float = 0.0,
                  ticker: str = "") -> int:
    return choose_candle_period(open_time, close_time, category, listing_vol, ticker)


def candle_time_params(open_time: str = "", close_time: str = "",
                       cap_end_to_now: bool = False) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    o = parse_iso(open_time)
    c = parse_iso(close_time)
    start = int((o - timedelta(hours=6)).timestamp()) if o else int((now - timedelta(days=90)).timestamp())
    end   = int((c + timedelta(hours=6)).timestamp()) if c else now_ts
    if cap_end_to_now:
        end = min(end, now_ts + 3600)
    if end <= start:
        end = start + 3600
    return {"start_ts": start, "end_ts": end}


def max_candle_span_seconds(period: int) -> int:
    """API returns at most ~10k candles per request; span = count × period minutes."""
    return MAX_CANDLES_PER_REQUEST * max(int(period), 1) * 60


def candle_volume_sum(candles: list) -> float:
    return sum(float(c.get("volume_fp") or c.get("volume") or 0) for c in candles)


def needs_volume_recovery(candle_vol: float, listing_vol: float) -> bool:
    if listing_vol < VOLUME_RECOVERY_MIN_LISTING:
        return False
    return candle_vol < listing_vol * VOLUME_RECOVERY_RATIO


def market_has_activity(market: dict) -> bool:
    for field in ("volume_fp", "volume_24h_fp", "open_interest_fp", "volume", "volume_24h"):
        val = market.get(field)
        if val is not None and float(val) > 0:
            return True
    return False


def _fetch_candles_chunked(path: str, period: int, open_time: str, close_time: str,
                           cap_end_to_now: bool = False) -> list:
    """Fetch candlesticks, splitting long ranges so each request stays under the API cap."""
    bounds = candle_time_params(open_time, close_time, cap_end_to_now=cap_end_to_now)
    start_ts, end_ts = bounds["start_ts"], bounds["end_ts"]
    span = max_candle_span_seconds(period)
    all_candles: list = []
    t = start_ts
    first = True
    while t < end_ts:
        chunk_end = min(t + span, end_ts)
        params = {
            "period_interval": period,
            "start_ts": t,
            "end_ts": chunk_end,
        }
        if not first:
            time.sleep(DELAY)
        data = get(path, params, timeout=CANDLE_TIMEOUT) or {}
        all_candles.extend(data.get("candlesticks", []))
        t = chunk_end
        first = False
    return all_candles


# ── Candlestick fetchers ──────────────────────────────────────────────────────

def fetch_candles_live(series_ticker: str, market_ticker: str,
                       open_time: str = "", close_time: str = "",
                       period: int | None = None,
                       category: str = "", listing_vol: float = 0.0) -> list:
    if period is None:
        period = choose_candle_period(
            open_time, close_time, category, listing_vol, market_ticker
        )
    path = f"/series/{series_ticker}/markets/{market_ticker}/candlesticks"
    return _fetch_candles_chunked(path, period, open_time, close_time, cap_end_to_now=True)


def fetch_candles_historical(market_ticker: str,
                              open_time: str = "", close_time: str = "",
                              period: int | None = None,
                              category: str = "", listing_vol: float = 0.0) -> list:
    if period is None:
        period = choose_candle_period(
            open_time, close_time, category, listing_vol, market_ticker
        )
    path = f"/historical/markets/{market_ticker}/candlesticks"
    return _fetch_candles_chunked(path, period, open_time, close_time, cap_end_to_now=False)


def iter_trade_pages(market_ticker: str, open_time: str = "", close_time: str = "",
                     max_pages: int = TRADE_FALLBACK_MAX_PAGES):
    """Yield trade pages from GET /markets/trades (avoids holding full history in RAM)."""
    params: dict = {"ticker": market_ticker, "limit": 1000}
    bounds = candle_time_params(open_time, close_time) if (open_time or close_time) else None
    if bounds:
        params["min_ts"] = bounds["start_ts"]
        params["max_ts"] = bounds["end_ts"]

    cursor = ""
    pages = 0
    while pages < max_pages:
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        data = get("/markets/trades", page_params, timeout=REQUEST_TIMEOUT)
        if data is None:
            if pages == 0 and ("min_ts" in params or "max_ts" in params):
                params.pop("min_ts", None)
                params.pop("max_ts", None)
                continue
            break
        items = data.get("trades", [])
        if not items:
            break
        yield items
        cursor = data.get("cursor", "") or ""
        pages += 1
        if not cursor:
            break
        time.sleep(DELAY)


def fetch_all_trades(market_ticker: str, open_time: str = "", close_time: str = "",
                     max_pages: int = TRADE_FALLBACK_MAX_PAGES) -> list:
    """Paginate GET /markets/trades for exact fill-level volume and fees."""
    trades: list = []
    for page in iter_trade_pages(market_ticker, open_time, close_time, max_pages=max_pages):
        trades.extend(page)
    return trades


def accumulate_trades_streaming(market_ticker: str, series: str, category: str,
                                fee_changes: dict, daily_series: dict,
                                min_date: str | None,
                                open_time: str = "", close_time: str = "") -> tuple[float, float]:
    """Accumulate fill fees page-by-page without retaining the full trade list."""
    total_contracts = 0.0
    total_fee = 0.0
    for page in iter_trade_pages(market_ticker, open_time, close_time):
        c, f = accumulate_trades(page, series, category, fee_changes, daily_series, min_date)
        total_contracts += c
        total_fee += f
    return total_contracts, total_fee

def _market_open_ts(meta: dict) -> int:
    o = parse_iso(meta.get("open_time", ""))
    return int(o.timestamp()) if o else 0


def _open_quarter_key(meta: dict) -> tuple[int, int]:
    o = parse_iso(meta.get("open_time", ""))
    if not o:
        return (0, 0)
    return (o.year, (o.month - 1) // 3)


def _batch_candle_window(chunk: list[str], ticker_meta: dict[str, dict]) -> dict[str, int]:
    starts, ends = [], []
    for ticker in chunk:
        meta = ticker_meta.get(ticker, {})
        bounds = candle_time_params(
            meta.get("open_time", ""),
            meta.get("close_time", ""),
            cap_end_to_now=True,
        )
        starts.append(bounds["start_ts"])
        ends.append(bounds["end_ts"])
    now_ts = int(datetime.now(timezone.utc).timestamp())
    end_ts = min(max(ends), now_ts + 3600)
    return {"start_ts": min(starts), "end_ts": end_ts}


def _fetch_candle_batch(chunk: list[str], period: int,
                        ticker_meta: dict[str, dict]) -> dict[str, list]:
    if not chunk:
        return {}
    window = _batch_candle_window(chunk, ticker_meta)
    params = {
        "market_tickers": ",".join(chunk),
        "period_interval": period,
        **window,
    }
    data, status = get_once("/markets/candlesticks", params, timeout=CANDLE_TIMEOUT)
    if status == 400 and len(chunk) > 1:
        mid = len(chunk) // 2
        left = _fetch_candle_batch(chunk[:mid], period, ticker_meta)
        right = _fetch_candle_batch(chunk[mid:], period, ticker_meta)
        left.update(right)
        return left
    if status == 400 or data is None:
        if len(chunk) == 1:
            ticker = chunk[0]
            meta = ticker_meta.get(ticker, {})
            series = meta.get("series_ticker", "")
            if series:
                return {ticker: fetch_candles_live(
                    series, ticker,
                    meta.get("open_time", ""), meta.get("close_time", ""),
                    category=meta.get("category", ""),
                    listing_vol=float(meta.get("listing_volume") or 0),
                )}
            print(f"    [warn] batch candle fetch failed for {ticker}", flush=True)
        return {}
    result = {}
    for entry in data.get("markets", []):
        tk = entry.get("market_ticker", "")
        cs = entry.get("candlesticks", [])
        if tk and cs:
            result[tk] = cs
    return result


def batch_fetch_candles_live(ticker_meta: dict[str, dict]) -> dict[str, list]:
    """
    Batch endpoint with small chunks. Split by period (1 vs 1440) because
    period_interval is a single parameter for the whole batch.
    Sports / high-volume markets use 1-min (not open→close duration alone).
    """
    one_min: list[str] = []
    daily: list[str] = []
    for t, m in ticker_meta.items():
        period = choose_candle_period(
            m.get("open_time", ""),
            m.get("close_time", ""),
            m.get("category", ""),
            float(m.get("listing_volume") or 0),
            t,
        )
        (one_min if period == 1 else daily).append(t)
    result: dict[str, list] = {}

    def _batch(tickers: list[str], period: int):
        by_quarter: dict[tuple[int, int], list[str]] = defaultdict(list)
        for ticker in tickers:
            by_quarter[_open_quarter_key(ticker_meta.get(ticker, {}))].append(ticker)
        for quarter_tickers in by_quarter.values():
            quarter_tickers.sort(key=lambda t: _market_open_ts(ticker_meta.get(t, {})))
            for i in range(0, len(quarter_tickers), CANDLE_BATCH_SIZE):
                chunk = quarter_tickers[i:i + CANDLE_BATCH_SIZE]
                result.update(_fetch_candle_batch(chunk, period, ticker_meta))
                time.sleep(DELAY)

    if one_min:
        print(f"       {len(one_min):,} markets → 1-min candles (sports/high-vol/short)", flush=True)
        _batch(one_min, 1)
    if daily:
        print(f"       {len(daily):,} markets → daily candles", flush=True)
        _batch(daily, 1440)

    return result


# ── Category inference ────────────────────────────────────────────────────────

TICKER_CATEGORY = {
    "KXNBA":"sports",   "KXNFL":"sports",    "KXNHL":"sports",
    "KXMLB":"sports",   "KXNCAA":"sports",   "KXSOCCER":"sports",
    "KXWC":"sports",    "KXUFC":"sports",    "KXNASCAR":"sports",
    "KXPGA":"sports",   "KXBIG":"sports",    "KXCHAMP":"sports",
    "KXMLS":"sports",   "KXCFL":"sports",    "KXATP":"sports",
    "KXMVE":"sports",   "KXMVESPORTS":"sports",
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
    if any(s.startswith(p) for p in MVE_SPORTS_MAKER_PREFIXES) or s.startswith("KXMVE"):
        return "sports"
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
        end_ts = normalize_ts(c.get("end_period_ts"))
        if end_ts is None:
            continue

        date_str = ts_to_date(end_ts)
        if min_date and date_str < min_date:
            continue

        candle_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
        vol = float(c.get("volume_fp") or c.get("volume") or 0)

        fee = candle_fee(c, series, candle_dt, fee_changes)

        if vol > 0:
            total_contracts += vol
            total_fee       += fee
            daily_series[date_str][category] += fee

    return total_contracts, total_fee


def accumulate_trades(trades: list, series: str, category: str,
                      fee_changes: dict, daily_series: dict,
                      min_date: str | None):
    """Accumulate exact fill fees from GET /markets/trades into daily_series."""
    total_contracts = 0.0
    total_fee = 0.0
    for trade in trades:
        created = parse_iso(trade.get("created_time", "") or "")
        if not created:
            continue
        date_str = created.strftime("%Y-%m-%d")
        if min_date and date_str < min_date:
            continue
        count = float(trade.get("count_fp") or trade.get("count") or 0)
        fee = trade_fee(trade, series, fee_changes)
        if count <= 0:
            continue
        total_contracts += count
        total_fee += fee
        daily_series[date_str][category] += fee
    return total_contracts, total_fee


# ── Checkpoint helpers ────────────────────────────────────────────────────────

def _load_daily_series(raw: dict) -> dict[str, dict[str, float]]:
    daily_series: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for date, cats in (raw or {}).items():
        for cat, fee in cats.items():
            daily_series[date][cat] = float(fee)
    return daily_series


def _serialize_daily_series(daily_series: dict) -> dict:
    return {d: {c: round(v, 6) for c, v in cats.items()} for d, cats in daily_series.items()}


def _empty_checkpoint() -> dict:
    return {
        "version": CHECKPOINT_VERSION,
        "live_complete": False,
        "live_index_complete": False,
        "historical_complete": False,
        "historical_cursor": "",
        "historical_page_ticker": "",
        "hist_count": 0,
        "live_count": 0,
        "live_next_index": 0,
        "skipped_zero_vol": 0,
        "no_candles": 0,
        "total_markets": 0,
        "total_contracts": 0.0,
        "total_fee": 0.0,
        "daily_series": {},
    }


def load_checkpoint(path: str | None) -> dict | None:
    if not path:
        return None
    checkpoint_path = Path(path).expanduser()
    if not checkpoint_path.exists():
        return None
    with checkpoint_path.open() as handle:
        data = json.load(handle)
    if data.get("version") != CHECKPOINT_VERSION:
        print(f"  ⚠ Checkpoint version mismatch at {checkpoint_path}; starting fresh.")
        return None
    return data


def save_checkpoint(path: str | None, state: dict):
    if not path:
        return
    checkpoint_path = Path(path).expanduser()
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["daily_series"] = _serialize_daily_series(
        _load_daily_series(payload.get("daily_series", {}))
    )
    tmp_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
    with tmp_path.open("w") as handle:
        json.dump(payload, handle)
    tmp_path.replace(checkpoint_path)
    print(f"  [checkpoint] saved → {checkpoint_path}", flush=True)


def _recover_market_activity(
    ticker: str,
    series: str,
    category: str,
    open_time: str,
    close_time: str,
    listing_vol: float,
    fee_changes: dict,
    daily_series: dict,
    min_date: str | None,
    *,
    live: bool = False,
    initial_candles: list | None = None,
    initial_period: int | None = None,
) -> tuple[float, float, bool]:
    """
    Candles → optional 1-min re-fetch → trades fallback.
    Returns (contracts, fee, no_usable_data).
    """
    period = initial_period
    if period is None:
        period = choose_candle_period(open_time, close_time, category, listing_vol, ticker)

    candles = list(initial_candles or [])
    if not candles:
        if live and series:
            candles = fetch_candles_live(
                series, ticker, open_time, close_time,
                period=period, category=category, listing_vol=listing_vol,
            )
        else:
            candles = fetch_candles_historical(
                ticker, open_time, close_time,
                period=period, category=category, listing_vol=listing_vol,
            )
        time.sleep(DELAY)

    vol = candle_volume_sum(candles)
    if needs_volume_recovery(vol, listing_vol) and period != 1:
        if live and series:
            candles = fetch_candles_live(
                series, ticker, open_time, close_time,
                period=1, category=category, listing_vol=listing_vol,
            )
        else:
            candles = fetch_candles_historical(
                ticker, open_time, close_time,
                period=1, category=category, listing_vol=listing_vol,
            )
        time.sleep(DELAY)
        vol = candle_volume_sum(candles)
        period = 1

    if needs_volume_recovery(vol, listing_vol) or not candles or vol <= 0:
        contracts, fee = accumulate_trades_streaming(
            ticker, series, category, fee_changes, daily_series, min_date,
            open_time, close_time,
        )
        time.sleep(DELAY)
        if contracts > 0:
            return contracts, fee, False

    if not candles:
        return 0.0, 0.0, True

    contracts, fee = accumulate_candles(
        candles, series, category, fee_changes, daily_series, min_date
    )
    # Free per-market candle buffer promptly (1-min series can be large).
    del candles
    if contracts == 0:
        return 0.0, 0.0, True
    return contracts, fee, False


def market_slim_meta(market: dict) -> dict:
    """Keep only fields needed for fee recovery (full market payloads OOMs on small VPS)."""
    return {
        "ticker": market.get("ticker", ""),
        "series_ticker": series_ticker_of(market),
        "category": market.get("category", "") or "",
        "open_time": market.get("open_time", "") or "",
        "close_time": market.get("close_time", "") or "",
        "listing_volume": listing_volume(market),
    }


def live_markets_index_path(checkpoint_path: str | None) -> Path | None:
    if not checkpoint_path:
        return None
    return Path(checkpoint_path).expanduser().parent / "live_markets_index.jsonl"


def build_live_markets_index(index_path: Path) -> tuple[int, int]:
    """
    Stream open markets (binary + MVE) to a slim JSONL index.
    Returns (active_count, scanned_count).
    """
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = index_path.with_suffix(index_path.suffix + ".tmp")
    seen: set[str] = set()
    active = 0
    scanned = 0
    with tmp_path.open("w") as out:
        for mve_filter in ("exclude", "only"):
            for m in paginate(
                "/markets", "markets",
                {"status": "open", "mve_filter": mve_filter},
            ):
                scanned += 1
                ticker = m.get("ticker") or ""
                if not ticker or ticker in seen or not market_has_activity(m):
                    continue
                seen.add(ticker)
                out.write(json.dumps(market_slim_meta(m), separators=(",", ":")) + "\n")
                active += 1
                if active % 10_000 == 0:
                    print(f"       indexed {active:,} active open markets "
                          f"(scanned {scanned:,})…", flush=True)
    tmp_path.replace(index_path)
    del seen
    gc.collect()
    return active, scanned


def count_jsonl_lines(path: Path) -> int:
    n = 0
    with path.open() as handle:
        for _ in handle:
            n += 1
    return n


def iter_jsonl_from(path: Path, start_idx: int = 0):
    with path.open() as handle:
        for i, line in enumerate(handle):
            if i < start_idx:
                continue
            line = line.strip()
            if not line:
                continue
            yield i, json.loads(line)


def _process_one_market(m: dict, fee_changes: dict, daily_series: dict,
                        min_date: str | None) -> tuple[float, float, bool, bool]:
    """Returns (contracts, fee, skipped_zero_vol, no_candles)."""
    ticker = m.get("ticker", "")
    open_time = m.get("open_time", "")
    close_time = m.get("close_time", "")
    api_cat = m.get("category", "")

    if min_date and close_time and close_time[:10] < min_date:
        return 0.0, 0.0, False, False

    listing = listing_volume(m)
    if listing <= 0:
        return 0.0, 0.0, True, False

    series = series_ticker_of(m)
    category = categorize(ticker, api_cat)
    contracts, fee, missing = _recover_market_activity(
        ticker, series, category, open_time, close_time, listing,
        fee_changes, daily_series, min_date, live=False,
    )
    if missing:
        return 0.0, 0.0, False, True
    return contracts, fee, False, False


# ── Part A: Event Contracts ───────────────────────────────────────────────────

def process_event_contracts(fee_changes: dict, min_date: str | None,
                             skip_live: bool = False,
                             checkpoint_path: str | None = None,
                             resume: bool = False) -> dict:
    print("\n══ PART A: EVENT CONTRACTS ══════════════════════════════════")

    checkpoint = _empty_checkpoint()
    if resume:
        loaded = load_checkpoint(checkpoint_path)
        if loaded:
            checkpoint.update(loaded)
            print(f"  [checkpoint] resuming ({checkpoint['total_markets']:,} markets, "
                  f"${checkpoint['total_fee']:,.0f} fees so far)")
        elif checkpoint_path:
            print(f"  [checkpoint] no file at {checkpoint_path}; starting fresh.")

    daily_series = _load_daily_series(checkpoint.get("daily_series", {}))
    total_markets = int(checkpoint.get("total_markets", 0))
    total_contracts = float(checkpoint.get("total_contracts", 0.0))
    total_fee = float(checkpoint.get("total_fee", 0.0))
    no_candles = int(checkpoint.get("no_candles", 0))
    skipped_zero_vol = int(checkpoint.get("skipped_zero_vol", 0))
    hist_count = int(checkpoint.get("hist_count", 0))
    live_count = int(checkpoint.get("live_count", 0))

    def _persist(extra: dict | None = None):
        state = {
            "version": CHECKPOINT_VERSION,
            "live_complete": checkpoint.get("live_complete", False),
            "live_index_complete": checkpoint.get("live_index_complete", False),
            "historical_complete": checkpoint.get("historical_complete", False),
            "historical_cursor": checkpoint.get("historical_cursor", ""),
            "historical_page_ticker": checkpoint.get("historical_page_ticker", ""),
            "hist_count": hist_count,
            "live_count": live_count,
            "live_next_index": checkpoint.get("live_next_index", 0),
            "skipped_zero_vol": skipped_zero_vol,
            "no_candles": no_candles,
            "total_markets": total_markets,
            "total_contracts": total_contracts,
            "total_fee": total_fee,
            "daily_series": daily_series,
        }
        if extra:
            state.update(extra)
        save_checkpoint(checkpoint_path, state)
        checkpoint.update(state)

    # ── A1: Live markets ──────────────────────────────────────────────────────
    if skip_live:
        print("\n  [A1] Skipping live markets (--skip-live).")
        checkpoint["live_complete"] = True
    elif checkpoint.get("live_complete"):
        print("\n  [A1] Live markets already complete (checkpoint).")
    else:
        print("\n  [A1] Fetching live markets with activity (binary + MVE)…")
        # Disk-backed slim index: holding full open-market payloads OOMs a 1GB VPS.
        index_path = live_markets_index_path(checkpoint_path)
        tmp_index: Path | None = None
        if index_path is None:
            fd, tmp_name = tempfile.mkstemp(prefix="live_markets_", suffix=".jsonl")
            os.close(fd)
            tmp_index = Path(tmp_name)
            index_path = tmp_index

        need_rebuild = (
            not checkpoint.get("live_index_complete")
            or not index_path.exists()
            or index_path.stat().st_size == 0
        )
        if need_rebuild:
            active, scanned = build_live_markets_index(index_path)
            print(f"       {active:,} active open markets (scanned {scanned:,})")
            checkpoint["live_index_complete"] = True
            checkpoint["live_next_index"] = 0
            _persist({
                "live_complete": False,
                "live_index_complete": True,
                "live_next_index": 0,
            })
        else:
            active = count_jsonl_lines(index_path)
            print(f"       reusing live index ({active:,} markets) at {index_path}")

        start_idx = int(checkpoint.get("live_next_index", 0))
        if start_idx:
            print(f"  [A1] Resuming live processing at market {start_idx:,}/{active:,}")

        chunk_meta: dict[str, dict] = {}
        chunk_tickers: list[str] = []
        chunk_start = start_idx

        def _flush_live_chunk():
            nonlocal live_count, total_markets, total_contracts, total_fee, no_candles
            nonlocal chunk_meta, chunk_tickers, chunk_start
            if not chunk_tickers:
                return
            end_idx = chunk_start + len(chunk_tickers)
            print(f"  [A1] Batch-fetching candlesticks ({chunk_start:,}–"
                  f"{end_idx:,} of {active:,})…", flush=True)
            candle_map = batch_fetch_candles_live(chunk_meta)
            for ticker in chunk_tickers:
                meta = chunk_meta[ticker]
                series = series_ticker_of(ticker, meta.get("series_ticker", ""))
                category = categorize(ticker, meta.get("category", ""))
                listing = float(meta.get("listing_volume") or 0)
                period = choose_candle_period(
                    meta.get("open_time", ""),
                    meta.get("close_time", ""),
                    category,
                    listing,
                    ticker,
                )
                contracts, fee, missing = _recover_market_activity(
                    ticker,
                    series,
                    category,
                    meta.get("open_time", ""),
                    meta.get("close_time", ""),
                    listing,
                    fee_changes,
                    daily_series,
                    min_date,
                    live=True,
                    initial_candles=candle_map.get(ticker, []),
                    initial_period=period,
                )
                if missing:
                    no_candles += 1
                    continue

                total_markets   += 1
                live_count      += 1
                total_contracts += contracts
                total_fee       += fee

                if live_count % 500 == 0:
                    print(f"       {live_count:,} live markets | fee ${total_fee:,.0f}", flush=True)

            checkpoint["live_next_index"] = end_idx
            _persist({"live_complete": False, "live_next_index": end_idx,
                      "live_index_complete": True})
            chunk_start = end_idx
            chunk_meta = {}
            chunk_tickers = []
            del candle_map
            gc.collect()

        for idx, meta in iter_jsonl_from(index_path, start_idx):
            ticker = meta.get("ticker") or ""
            if not ticker:
                continue
            chunk_tickers.append(ticker)
            chunk_meta[ticker] = meta
            if len(chunk_tickers) >= LIVE_PROCESS_CHUNK:
                _flush_live_chunk()
        _flush_live_chunk()

        checkpoint["live_complete"] = True
        checkpoint["live_next_index"] = active
        _persist({"live_complete": True, "live_next_index": active,
                  "live_index_complete": True})
        if tmp_index is not None:
            try:
                tmp_index.unlink(missing_ok=True)
            except OSError:
                pass
        print(f"  [A1] Done. {live_count:,} live markets processed.")

    # ── A2: Historical markets (resumable pagination) ─────────────────────────
    historical_complete = bool(checkpoint.get("historical_complete"))
    if historical_complete:
        print("\n  [A2] Historical markets already complete (checkpoint).")
    else:
        print("\n  [A2] Fetching historical markets…")
        cursor = checkpoint.get("historical_cursor", "")
        resume_ticker = checkpoint.get("historical_page_ticker", "")
        pages = 0
        skipping = bool(resume_ticker)

        while pages < MAX_PAGES:
            page = fetch_page("/historical/markets", "markets", cursor=cursor)
            if page is None:
                _persist({
                    "historical_complete": False,
                    "historical_cursor": cursor,
                    "historical_page_ticker": resume_ticker,
                })
                print("  [A2] Pagination interrupted — checkpoint saved; re-run with --resume")
                break

            items = page.get("markets", [])
            if not items:
                historical_complete = True
                checkpoint["historical_complete"] = True
                checkpoint["historical_cursor"] = ""
                checkpoint["historical_page_ticker"] = ""
                _persist()
                break

            for m in items:
                ticker = m.get("ticker", "")
                if skipping:
                    if ticker != resume_ticker:
                        continue
                    skipping = False
                    continue

                contracts, fee, skipped, missing = _process_one_market(
                    m, fee_changes, daily_series, min_date
                )
                if skipped:
                    skipped_zero_vol += 1
                    checkpoint["historical_page_ticker"] = ticker
                    continue
                if missing:
                    no_candles += 1
                    checkpoint["historical_page_ticker"] = ticker
                    continue

                total_markets += 1
                hist_count += 1
                total_contracts += contracts
                total_fee += fee
                checkpoint["historical_page_ticker"] = ticker

                if hist_count % CHECKPOINT_EVERY == 0:
                    print(f"       {hist_count:,} historical | fee ${total_fee:,.0f}", flush=True)
                    _persist({"historical_complete": False, "historical_cursor": cursor})

            next_cursor = page.get("cursor", "")
            pages += 1
            if not next_cursor:
                historical_complete = True
                checkpoint["historical_complete"] = True
                checkpoint["historical_cursor"] = ""
                checkpoint["historical_page_ticker"] = ""
                _persist()
                break

            cursor = next_cursor
            checkpoint["historical_cursor"] = cursor
            checkpoint["historical_page_ticker"] = ""
            resume_ticker = ""
            skipping = False
            _persist({"historical_complete": False})
            time.sleep(DELAY)

        if pages >= MAX_PAGES:
            print(f"\n  ⚠ WARNING: historical pagination ceiling hit ({hist_count:,} markets)\n",
                  flush=True)

        print(f"  [A2] Done. {hist_count:,} historical markets processed.")
        if skipped_zero_vol:
            print(f"  Note: {skipped_zero_vol:,} zero-volume markets skipped (no candle fetch).")
        if no_candles:
            print(f"  Note: {no_candles:,} markets had no usable candle data.")

    return {
        "total_markets": total_markets,
        "total_contracts": total_contracts,
        "total_fee": total_fee,
        "daily_series": {d: dict(cats) for d, cats in daily_series.items()},
        "historical_complete": historical_complete,
        "live_complete": bool(checkpoint.get("live_complete")),
        "skip_live": skip_live,
        "hist_count": hist_count,
        "live_count": live_count,
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


# ── Scan validation ───────────────────────────────────────────────────────────

def validate_scan_quality(event: dict) -> list[str]:
    """Return human-readable warnings when a scan looks incomplete or biased."""
    warnings: list[str] = []
    dates = sorted(event.get("daily_series", {}))
    hist_count = int(event.get("hist_count", 0))
    total_markets = int(event.get("total_markets", 0))

    if not event.get("historical_complete", True):
        warnings.append("Historical pagination did not finish — re-run with --resume")
    if event.get("skip_live"):
        warnings.append("Live markets were skipped (--skip-live); totals exclude open-market volume")
    elif not event.get("live_complete", True):
        warnings.append("Live market pass did not complete")

    if hist_count < MIN_EXPECTED_MARKETS:
        warnings.append(
            f"Only {hist_count:,} historical markets processed (expected {MIN_EXPECTED_MARKETS:,}+)"
        )
    if len(dates) < MIN_EXPECTED_DATE_SPAN:
        warnings.append(
            f"Only {len(dates)} distinct fee dates (expected {MIN_EXPECTED_DATE_SPAN}+ spanning years)"
        )
    if dates and dates[0] > "2020-01-01":
        warnings.append(
            f"Earliest fee date is {dates[0]} — likely a recency-biased partial scan"
        )
    if total_markets and event.get("total_fee", 0) / total_markets < 5:
        warnings.append(
            f"Average fee/market is only ${event['total_fee']/total_markets:.2f} — verify date span"
        )
    return warnings


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
    print("  - Candle price: mean_dollars / legacy mean (mean traded YES price)")
    print("  - Trade-level alternative: GET /markets/trades (yes_price_dollars per fill)")
    print("  - Intraday markets (<24h): 1-min candles; multi-day: daily candles")
    print("  - ceil() rounding per-market (vs per-fill — slight undercount)")
    print("  - Zero-fee series excluded: KXBTCY, KXCITRINI, KXDOED")
    print("  - Perp fees: bps defaults used; authenticated actual rates are not queried")
    print("  - Funding rate excluded (trader-to-trader, not to Kalshi)")
    print("═"*52)

    scan_warnings = validate_scan_quality(event)
    if scan_warnings:
        print("\n  SCAN QUALITY WARNINGS:")
        for warning in scan_warnings:
            print(f"    ⚠ {warning}")
        print()

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
    return scan_warnings


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
    parser.add_argument("--checkpoint", default=None,
                        help="Checkpoint JSON path for resumable scans (e.g. data/checkpoints/scan.json)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from --checkpoint if present")
    parser.add_argument("--fail-on-incomplete", action="store_true",
                        help="Exit with code 2 when scan quality validation fails")
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
        fee_changes, min_date, skip_live=args.skip_live,
        checkpoint_path=args.checkpoint, resume=args.resume,
    )

    perp_results = {"total_fee": 0.0, "total_notional": 0.0,
                    "daily_series": {}, "note": "skipped",
                    "all_time_adjustment": False}
    if not args.skip_perps:
        perp_results = process_perps(min_date)

    scan_warnings = print_and_save(event_results, perp_results, args.output_dir)
    if args.fail_on_incomplete and scan_warnings:
        print("  Exiting with code 2 (--fail-on-incomplete): scan quality checks failed.")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
