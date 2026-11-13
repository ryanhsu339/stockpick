#!/usr/bin/env python3
"""
stock_price.py — fetch stock price history for the Dash app's price chart.

SEC EDGAR (used by company_growth_calc.py) has no price data, so this pulls
from Yahoo Finance via yfinance. Kept separate from company_growth_calc.py
since it's a different data source with its own failure modes (Yahoo
symbols can differ slightly from SEC tickers, e.g. share classes).
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from yfinance.data import YfData


class PriceDataError(Exception):
    """Raised when price history can't be fetched or is empty."""


class HoldingsDataError(Exception):
    """Raised when a fund's top-holdings breakdown can't be fetched or is empty."""


# range key -> yfinance kwargs. Intraday ranges use fine intervals (Yahoo
# only retains 1m data for ~7 days and other intraday data for ~60 days,
# which is why 1D/5D/1M each need a different granularity). "3Y" isn't a
# valid yfinance `period` string, so it's built from explicit start/end;
# "YTD" isn't one either (yfinance has "ytd" for calendar-quarter-based
# tickers only), so it's built from Jan 1 of the current year instead.
_RANGE_CONFIG = {
    # Two days with pre/after-hours bars (same single request -- prepost is
    # just a flag on Yahoo's chart call): before the open, today has only
    # pre-market bars, so the second day keeps the last regular session in
    # reach for callers that want regular hours only. See _select_session.
    "1D": {"period": "2d", "interval": "5m", "prepost": True},
    "5D": {"period": "5d", "interval": "15m"},
    "1M": {"period": "1mo", "interval": "1d"},
    "3M": {"period": "3mo", "interval": "1d"},
    "6M": {"period": "6mo", "interval": "1d"},
    "YTD": {"ytd": True, "interval": "1d"},
    "1Y": {"period": "1y", "interval": "1d"},
    "3Y": {"years_back": 3, "interval": "1d"},
    "ALL": {"period": "max", "interval": "1d"},
}

RANGE_KEYS = ["1D", "5D", "1M", "3M", "6M", "YTD", "1Y", "3Y", "ALL"]
# Ranges whose data moves minute to minute -- the only ones the price chart
# re-polls (see dash_app.py's gate_price_chart_refresh).
LIVE_RANGE_KEYS = ("1D", "5D")

# Price history per (ticker, range), shared by every visitor: each open
# chart re-polls every 15s, so without this N people watching one ticker
# meant N identical Yahoo requests per tick, all from the server's one IP
# (a rate-limit risk for everything else that uses Yahoo). Intraday ranges
# stay as fresh as the chart's own refresh; daily-bar ranges barely change.
_PRICE_CACHE_TTL = {"1D": 15, "5D": 15}
_PRICE_CACHE_TTL_DEFAULT = 300
_PRICE_CACHE_MAX = 500
_price_cache = {}  # (TICKER, range_key) -> (fetched_at, DataFrame)
_price_cache_lock = threading.Lock()
# One lock per key, so concurrent requests for the same ticker/range wait
# for a single in-flight Yahoo fetch instead of each making their own.
_price_fetch_locks = {}


def fetch_price_history(ticker, range_key, extended=False):
    """Return a DataFrame with a DatetimeIndex and 'Close'/'Volume' columns
    for `ticker` over `range_key` (one of RANGE_KEYS). Raises PriceDataError
    if Yahoo Finance has no data for this ticker/range. Briefly cached --
    see _price_cache.

    1D only: by default just the latest regular session's bars; with
    `extended=True`, every bar of the latest trading day -- pre-market and
    after-hours included -- plus a boolean 'Extended' column marking them."""
    return _select_session(_fetch_price_history_cached(ticker, range_key), range_key, extended)


def _select_session(df, range_key, extended):
    if "Extended" not in df.columns:
        return df[["Close", "Volume"]]
    days = df.index.date
    if extended:
        return df[days == days[-1]]
    regular = df[~df["Extended"]]
    if regular.empty:
        raise PriceDataError(f"No regular-session price data available ({range_key}).")
    regular = regular[regular.index.date == regular.index[-1].date()]
    return regular[["Close", "Volume"]]


def _fetch_price_history_cached(ticker, range_key):
    key = (ticker.upper(), range_key)
    ttl = _PRICE_CACHE_TTL.get(range_key, _PRICE_CACHE_TTL_DEFAULT)
    with _price_cache_lock:
        cached = _price_cache.get(key)
        if cached and time.time() - cached[0] < ttl:
            return cached[1].copy()
        fetch_lock = _price_fetch_locks.setdefault(key, threading.Lock())
    with fetch_lock:
        # Another request may have filled it while this one waited.
        with _price_cache_lock:
            cached = _price_cache.get(key)
            if cached and time.time() - cached[0] < ttl:
                return cached[1].copy()
        df = _fetch_price_history_uncached(ticker, range_key)
        with _price_cache_lock:
            _price_cache[key] = (time.time(), df)
            if len(_price_cache) > _PRICE_CACHE_MAX:
                now = time.time()
                for k, (at, _) in list(_price_cache.items()):
                    if now - at >= _PRICE_CACHE_TTL.get(k[1], _PRICE_CACHE_TTL_DEFAULT):
                        del _price_cache[k]
                        _price_fetch_locks.pop(k, None)
        return df.copy()


def _fetch_price_history_uncached(ticker, range_key):
    config = _RANGE_CONFIG.get(range_key)
    if config is None:
        raise PriceDataError(f"Unknown range '{range_key}'.")

    kwargs = {"interval": config["interval"], "auto_adjust": True}
    if config.get("prepost"):
        kwargs["prepost"] = True
    if "years_back" in config:
        end = date.today()
        start = end - timedelta(days=365 * config["years_back"])
        kwargs["start"] = start.isoformat()
        kwargs["end"] = end.isoformat()
    elif config.get("ytd"):
        today = date.today()
        kwargs["start"] = date(today.year, 1, 1).isoformat()
        kwargs["end"] = today.isoformat()
    else:
        kwargs["period"] = config["period"]

    try:
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(**kwargs)
    except Exception as e:
        raise PriceDataError(f"Could not fetch price data for {ticker}: {e}") from e

    if df is None or df.empty or "Close" not in df.columns:
        raise PriceDataError(f"No price data available for {ticker} ({range_key}).")

    df = df[~df["Close"].isna()]
    if df.empty:
        raise PriceDataError(f"No price data available for {ticker} ({range_key}).")

    if "Volume" not in df.columns:
        df = df.assign(Volume=None)

    if not config.get("prepost"):
        return df[["Close", "Volume"]]
    return df[["Close", "Volume"]].assign(Extended=_extended_mask(df.index, ticker_obj.history_metadata))


def _extended_mask(index, metadata):
    """True for bars outside each day's regular session, per Yahoo's own
    tradingPeriods (so non-US exchanges' hours are right too). All False
    when Yahoo doesn't say -- e.g. crypto, which has no pre/post sessions."""
    periods = (metadata or {}).get("tradingPeriods")
    mask = pd.Series(False, index=index)
    if not isinstance(periods, pd.DataFrame) or not {"start", "end"} <= set(periods.columns):
        return mask.values
    for _, row in periods.iterrows():
        start, end = row["start"], row["end"]
        same_day = index.date == start.date()
        mask[same_day & ((index < start) | (index >= end))] = True
    return mask.values


# fetch_ticker_overview results, briefly: the KPI tiles render once as soon
# as a ticker is picked and again when its SEC data (ROIC) arrives a moment
# later, and the earnings chart reads the fiscal year-end from the same
# overview -- without this, each of those would be its own Yahoo round trip.
_OVERVIEW_CACHE_TTL = 120
_overview_cache = {}


def fetch_ticker_overview(ticker):
    """Return a dict of yfinance's own overview stats for `ticker` (market
    cap, trailing P/E, trailing-twelve-month revenue, net margin, exchange,
    sector, industry, business summary, analyst price target and buy/hold/sell split,
    last fiscal year-end as an ISO date) for the Company Tracker's KPI tiles
    and description. Individual
    fields come back None when yfinance doesn't have them for this ticker
    (e.g. ETFs have no P/E or analyst coverage) -- only raises if the ticker
    itself can't be resolved at all."""
    key = ticker.upper()
    cached = _overview_cache.get(key)
    if cached and time.time() - cached[0] < _OVERVIEW_CACHE_TTL:
        return cached[1]
    overview = _fetch_ticker_overview_uncached(ticker)
    _overview_cache[key] = (time.time(), overview)
    if len(_overview_cache) > 200:
        for stale in [k for k, (at, _) in _overview_cache.items() if time.time() - at >= _OVERVIEW_CACHE_TTL]:
            del _overview_cache[stale]
    return overview


def _fetch_ticker_overview_uncached(ticker):
    try:
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.info
    except Exception as e:
        raise PriceDataError(f"Could not fetch overview data for {ticker}: {e}") from e
    if not info:
        raise PriceDataError(f"No overview data available for {ticker}.")
    return {
        "market_cap": info.get("marketCap"),
        "trailing_pe": info.get("trailingPE"),
        "revenue_ttm": info.get("totalRevenue"),
        "net_margin": info.get("profitMargins"),
        "exchange": info.get("fullExchangeName") or info.get("exchange"),
        "sector": info.get("sector"),
        "business_summary": info.get("longBusinessSummary"),
        "industry": info.get("industry"),
        "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "target_mean_price": info.get("targetMeanPrice"),
        "fiscal_year_end": (date.fromtimestamp(info["lastFiscalYearEnd"]).isoformat()
                            if info.get("lastFiscalYearEnd") else None),
        **_fetch_analyst_split(ticker_obj),
    }


# fetch_put_call_ratio results per ticker. Open interest is only updated
# once a day (after the previous session settles), so an hour is plenty --
# and it keeps this to a handful of Yahoo requests per ticker per hour.
_PUT_CALL_CACHE_TTL = 3600
_PUT_CALL_WINDOW_DAYS = 30
_put_call_cache = {}


def fetch_put_call_ratio(ticker):
    """{"ratio", "call_oi", "put_oi"}: put/call open interest summed over
    every option expiration in the next 30 days -- one Yahoo request for
    the expiration list plus one per expiration (typically 4-8). All None
    when the ticker has no listed options or Yahoo fails; never raises."""
    key = ticker.upper()
    cached = _put_call_cache.get(key)
    if cached and time.time() - cached[0] < _PUT_CALL_CACHE_TTL:
        return cached[1]
    result = _fetch_put_call_ratio_uncached(ticker)
    _put_call_cache[key] = (time.time(), result)
    if len(_put_call_cache) > 200:
        for stale in [k for k, (at, _) in _put_call_cache.items() if time.time() - at >= _PUT_CALL_CACHE_TTL]:
            del _put_call_cache[stale]
    return result


def _fetch_put_call_ratio_uncached(ticker):
    empty = {"ratio": None, "call_oi": None, "put_oi": None}
    try:
        ticker_obj = yf.Ticker(ticker)
        horizon = date.today() + timedelta(days=_PUT_CALL_WINDOW_DAYS)
        expirations = [e for e in ticker_obj.options if date.fromisoformat(e) <= horizon]
        call_oi = put_oi = 0
        for expiration in expirations:
            chain = ticker_obj.option_chain(expiration)
            call_oi += int(chain.calls["openInterest"].fillna(0).sum())
            put_oi += int(chain.puts["openInterest"].fillna(0).sum())
    except Exception:
        return empty
    if not call_oi:
        return empty
    return {"ratio": put_oi / call_oi, "call_oi": call_oi, "put_oi": put_oi}


def _fetch_analyst_split(ticker_obj):
    """{"pct_buy", "pct_hold", "pct_sell"} (0-1 fractions) plus
    "rating_count" from Yahoo's current-month recommendation counts, with
    strong buy/sell folded into buy/sell. A separate Yahoo request from
    .info, so it's best-effort on its own -- everything comes back None
    rather than failing the overview."""
    empty = {"pct_buy": None, "pct_hold": None, "pct_sell": None, "rating_count": None}
    try:
        recs = ticker_obj.recommendations
    except Exception:
        return empty
    if recs is None or recs.empty:
        return empty
    # Row "0m" is the current month; fall back to the first row if Yahoo
    # ever stops labeling periods.
    current = recs[recs["period"] == "0m"] if "period" in recs.columns else recs
    row = (current if not current.empty else recs).iloc[0]
    buy = row.get("strongBuy", 0) + row.get("buy", 0)
    hold = row.get("hold", 0)
    sell = row.get("sell", 0) + row.get("strongSell", 0)
    total = buy + hold + sell
    if not total:
        return empty
    return {"pct_buy": buy / total, "pct_hold": hold / total, "pct_sell": sell / total,
            "rating_count": int(total)}


# Parsed earnings results per ticker -- a few small dicts each, instead of
# the ~1MB Yahoo calendar page they're scraped from. Short-lived enough to
# pick up a fresh report the same day; capped so it can't grow unbounded.
_EARNINGS_CACHE_TTL = 6 * 3600
_EARNINGS_CACHE_MAX = 500
_earnings_cache = {}  # (ticker, quarters) -> (fetched_at, result list or error message)
_earnings_cache_lock = threading.Lock()


def _drop_yfinance_response_cache():
    """get_earnings_dates scrapes Yahoo's earnings-calendar HTML page
    (~1MB) through yfinance's process-wide response cache (an lru_cache of
    up to 64 whole HTTP responses, never expired) -- so every new ticker
    viewed left another page in memory for good, a staircase on the
    server's memory graph, and a cached page also never picked up a newly
    reported quarter. Only the parsed result is kept (_earnings_cache)."""
    clear = getattr(getattr(YfData, "cache_get", None), "cache_clear", None)
    if clear:
        clear()


def fetch_earnings_history(ticker, quarters=8):
    """Return a list of {"date", "estimate", "actual"} dicts, oldest first,
    for `ticker`'s last `quarters` reported quarters plus the next upcoming
    report (actual None) when Yahoo has one scheduled. "date" is the
    report's calendar date; EPS figures are Yahoo's adjusted (non-GAAP)
    numbers, the same basis analysts' consensus estimates use. Raises
    PriceDataError if Yahoo has no earnings history (funds, recent IPOs)."""
    key = (ticker.upper(), quarters)
    cached = _earnings_cache.get(key)
    if cached and time.time() - cached[0] < _EARNINGS_CACHE_TTL:
        if isinstance(cached[1], str):
            raise PriceDataError(cached[1])
        return cached[1]
    try:
        result = _fetch_earnings_history_uncached(ticker, quarters)
    except PriceDataError as e:
        # Remember "no history" (funds, recent IPOs) so those don't refetch
        # the page on every view -- but not a failed fetch, which may just
        # be a transient Yahoo/network error worth retrying next time.
        if str(e).startswith("No earnings history"):
            _store_earnings(key, str(e))
        raise
    _store_earnings(key, result)
    return result


def _store_earnings(key, value):
    with _earnings_cache_lock:
        _earnings_cache[key] = (time.time(), value)
        if len(_earnings_cache) > _EARNINGS_CACHE_MAX:
            oldest = min(_earnings_cache, key=lambda k: _earnings_cache[k][0])
            del _earnings_cache[oldest]


def _fetch_earnings_history_uncached(ticker, quarters):
    try:
        # limit counts upcoming rows too, so ask for a few spare.
        df = yf.Ticker(ticker).get_earnings_dates(limit=quarters + 4)
    except Exception as e:
        raise PriceDataError(f"Could not fetch earnings history for {ticker}: {e}") from e
    finally:
        _drop_yfinance_response_cache()
    if df is None or df.empty or "EPS Estimate" not in df.columns:
        raise PriceDataError(f"No earnings history available for {ticker}.")

    def num(v):
        return None if v is None or v != v else float(v)  # v != v: NaN

    events = [
        {"date": ts.date(), "estimate": num(row.get("EPS Estimate")), "actual": num(row.get("Reported EPS"))}
        for ts, row in df.sort_index().iterrows()
    ]
    today = date.today()
    reported = [e for e in events if e["actual"] is not None]
    upcoming = [e for e in events if e["actual"] is None and e["date"] >= today and e["estimate"] is not None]
    result = reported[-quarters:] + upcoming[:1]
    if not reported:
        raise PriceDataError(f"No earnings history available for {ticker}.")
    return result


def fetch_fund_name(ticker):
    """Best-effort real name for an ETF/mutual fund ticker. SEC's own fund
    ticker map (see load_ticker_map in company_growth_calc.py) only carries
    cik/series/class/symbol, no name at all, so the search/suggestion UI
    falls back to this for a real label instead of just repeating the
    ticker. Returns None (never raises) if yfinance has nothing for it --
    callers fall back to a generic label."""
    try:
        info = yf.Ticker(ticker).info
    except Exception:
        return None
    return info.get("longName") or info.get("shortName") or None


def fetch_top_holdings(ticker, limit=10):
    """Return a DataFrame of `ticker`'s top holdings (columns: symbol, name,
    holding_pct) for an ETF/mutual fund, via yfinance's fund data. Raises
    HoldingsDataError if `ticker` isn't a fund or Yahoo has no breakdown for
    it."""
    try:
        ticker_obj = yf.Ticker(ticker)
        # yfinance has exposed this as either a property or a get_*() method
        # across versions -- try both rather than pinning to one.
        funds_data = getattr(ticker_obj, "funds_data", None)
        if funds_data is None and hasattr(ticker_obj, "get_funds_data"):
            funds_data = ticker_obj.get_funds_data()
        holdings = getattr(funds_data, "top_holdings", None) if funds_data else None
    except Exception as e:
        raise HoldingsDataError(f"Could not fetch holdings for {ticker}: {e}") from e

    if holdings is None or holdings.empty:
        raise HoldingsDataError(f"No holdings data available for {ticker}.")

    df = holdings.reset_index()
    # yfinance names these "Symbol" (from the index)/"Name"/"Holding Percent"
    # as of this writing, but rename positionally too as a defensive fallback
    # in case a future version changes the exact labels.
    rename = {}
    if "Symbol" in df.columns:
        rename["Symbol"] = "symbol"
    elif len(df.columns) >= 1:
        rename[df.columns[0]] = "symbol"
    if "Name" in df.columns:
        rename["Name"] = "name"
    elif len(df.columns) >= 2:
        rename[df.columns[1]] = "name"
    if "Holding Percent" in df.columns:
        rename["Holding Percent"] = "holding_pct"
    elif len(df.columns) >= 3:
        rename[df.columns[2]] = "holding_pct"
    df = df.rename(columns=rename)

    if not {"symbol", "name", "holding_pct"}.issubset(df.columns):
        raise HoldingsDataError(f"Unexpected holdings data format for {ticker}.")

    df["holding_pct"] = df["holding_pct"].astype(float) * 100
    return df[["symbol", "name", "holding_pct"]].head(limit)


# fetch_previous_close results per (ticker, session date) -- the 1D price
# chart re-renders every 15s (price-chart-refresh), but the prior session's
# close doesn't change during the day.
_PREV_CLOSE_CACHE_TTL = 3600
_prev_close_cache = {}


def fetch_previous_close(ticker, session_date):
    """Official close of the last trading session before `session_date` (a
    datetime.date) -- the baseline for a "day's change", matching how Yahoo's
    movers screens and every quote page measure it. The 1D chart's own first
    5-minute bar is the wrong baseline: on a gap-up/down day it misses the
    whole overnight move. Returns None if Yahoo has no such session."""
    key = (ticker.upper(), session_date)
    cached = _prev_close_cache.get(key)
    if cached and time.time() - cached[0] < _PREV_CLOSE_CACHE_TTL:
        return cached[1]
    try:
        df = yf.Ticker(ticker).history(period="10d", interval="1d", auto_adjust=True)
    except Exception:
        return None
    prev = None
    if df is not None and "Close" in df:
        earlier = df[[d.date() < session_date for d in df.index]]["Close"].dropna()
        if len(earlier):
            prev = float(earlier.iloc[-1])
    _prev_close_cache[key] = (time.time(), prev)
    return prev


# fetch_day_change_pcts results per ticker -- the sidebar watchlist asks
# again on every re-render and on its own 5-minute refresh, and a few
# minutes' staleness is fine for a glanceable "day's return".
_DAY_CHANGE_CACHE_TTL = 120
_day_change_cache = {}


def _fetch_day_change_pct_uncached(ticker):
    # Last two daily closes: during market hours Yahoo's last daily bar is
    # today's so-far price, so this is today's move vs. yesterday's close;
    # outside market hours it's the most recent session's move.
    df = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=True)
    closes = df["Close"].dropna() if df is not None and "Close" in df else []
    if len(closes) < 2 or not closes.iloc[-2]:
        return None
    return float((closes.iloc[-1] / closes.iloc[-2] - 1) * 100)


def fetch_day_change_pcts(tickers, max_workers=6):
    """{ticker: today's % change (or the last session's), or None if Yahoo
    has no data} for each of `tickers`, fetched in parallel. Never raises --
    a ticker that fails just maps to None."""
    now = time.time()
    result, missing = {}, []
    for t in dict.fromkeys(t.upper() for t in tickers):
        cached = _day_change_cache.get(t)
        if cached and now - cached[0] < _DAY_CHANGE_CACHE_TTL:
            result[t] = cached[1]
        else:
            missing.append(t)

    def fetch(t):
        try:
            return t, _fetch_day_change_pct_uncached(t)
        except Exception:
            return t, None

    if missing:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(missing))) as pool:
            for t, pct in pool.map(fetch, missing):
                result[t] = pct
                _day_change_cache[t] = (now, pct)
    return result


# Yahoo's predefined "day_gainers"/"day_losers" screens -- already filtered
# to US-listed stocks with real size/liquidity (roughly $2B+ market cap,
# $5+ price), so a penny stock's 300% pop doesn't crowd out everything
# else. Cached briefly since every visit to the Companies tab would
# otherwise re-hit Yahoo for what's effectively the same list.
_MOVERS_SCREENS = {"gainers": "day_gainers", "losers": "day_losers"}
_MOVERS_CACHE_TTL = 300
_movers_cache = {}


def fetch_day_movers(kind, count=100):
    """Return today's top `count` gainers or losers (`kind` is "gainers"
    or "losers") as a list of dicts (symbol, name, price, change,
    change_pct, market_cap, volume), biggest move first. Raises
    PriceDataError if Yahoo's screener can't be reached."""
    screen = _MOVERS_SCREENS[kind]
    cached = _movers_cache.get(kind)
    if cached and time.time() - cached[0] < _MOVERS_CACHE_TTL:
        return cached[1]
    try:
        quotes = (yf.screen(screen, count=count) or {}).get("quotes") or []
    except Exception as e:
        raise PriceDataError(f"Could not fetch day {kind}: {e}") from e
    movers = [
        {
            "symbol": q["symbol"],
            "name": q.get("shortName") or q.get("longName") or q["symbol"],
            "price": q.get("regularMarketPrice"),
            "change": q.get("regularMarketChange"),
            "change_pct": q.get("regularMarketChangePercent"),
            "market_cap": q.get("marketCap"),
            "volume": q.get("regularMarketVolume"),
        }
        for q in quotes
        if q.get("symbol") and q.get("regularMarketChangePercent") is not None
    ]
    movers.sort(key=lambda m: m["change_pct"], reverse=(kind == "gainers"))
    _movers_cache[kind] = (time.time(), movers)
    return movers


# Written by build_sp500_snapshot.py: one {symbol, sector, industry} per
# company in the index.
SP500_SNAPSHOT_PATH = Path(__file__).parent / "data" / "sp500_constituents.json"
_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
_QUOTE_BATCH_SIZE = 200
_SP500_QUOTE_FIELDS = "marketCap,regularMarketChangePercent,regularMarketPrice,floatShares"
_SP500_CACHE_TTL = 600
_sp500_cache = None
# Not in the index, but quoted in the same requests for the Companies tab's
# ticker bar (oil, natural gas, gold, silver and copper ETFs), which costs no
# extra calls while the total stays within the same batches.
SP500_EXTRA_SYMBOLS = ("USO", "UNG", "GLD", "SLV", "CPER")
_MARKET_TZ = ZoneInfo("America/New_York")


def _sp500_index_weight(quote):
    """A stock's float-adjusted market cap as of the previous close, the same
    basis the index weights by: a cap that already includes today's move would
    overweight the day's winners. Uses the full market cap if Yahoo has no float."""
    cap = quote["marketCap"]
    if quote.get("floatShares") and quote.get("regularMarketPrice"):
        cap = min(cap, quote["floatShares"] * quote["regularMarketPrice"])
    return cap / (1 + quote["regularMarketChangePercent"] / 100)


def _session_over_before(fetched_at, now):
    """Whether no US session has run since `fetched_at` (a timestamp), so
    quotes fetched then are still the latest. `now` is an aware datetime in
    _MARKET_TZ. Holidays and early closes count as normal sessions, which only
    costs a few needless refreshes. The session runs to 16:15 here, not 16:00,
    so the last fetch of the day picks up the closing auction's final prices."""
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close = now.replace(hour=16, minute=15, second=0, microsecond=0)
    if now.weekday() < 5 and market_open <= now < close:
        return False
    while close.weekday() >= 5 or close > now:
        close -= timedelta(days=1)
    return fetched_at >= close.timestamp()


def us_market_session_active(grace_minutes=0):
    """Whether a regular US session is running now -- to 16:15, as
    _session_over_before has it -- or ended within the last `grace_minutes`.
    Holidays count as normal weekdays."""
    now = datetime.now(_MARKET_TZ)
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end = now.replace(hour=16, minute=15, second=0, microsecond=0) + timedelta(minutes=grace_minutes)
    return now.weekday() < 5 and start <= now < end


def fetch_sp500_quotes():
    """Return (companies, quotes): the constituents snapshot's
    {symbol, sector, industry} rows, and Yahoo's quote dict per symbol for
    those plus SP500_EXTRA_SYMBOLS. Cached, and not refetched once a session
    has closed (_session_over_before). Raises PriceDataError if the snapshot or
    Yahoo's quotes can't be read."""
    global _sp500_cache
    if _sp500_cache and (time.time() - _sp500_cache[0] < _SP500_CACHE_TTL
                         or _session_over_before(_sp500_cache[0], datetime.now(_MARKET_TZ))):
        return _sp500_cache[1]
    try:
        companies = json.loads(SP500_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        symbols = [c["symbol"] for c in companies] + list(SP500_EXTRA_SYMBOLS)
        # Yahoo's quote endpoint is the one call that returns these fields for
        # hundreds of symbols at once; yfinance has no public wrapper for it,
        # so it goes through the same authenticated YfData session yfinance uses.
        quotes = {}
        for i in range(0, len(symbols), _QUOTE_BATCH_SIZE):
            response = YfData().get(_QUOTE_URL, params={"symbols": ",".join(symbols[i:i + _QUOTE_BATCH_SIZE]),
                                                        "fields": _SP500_QUOTE_FIELDS})
            for q in response.json()["quoteResponse"]["result"]:
                quotes[q["symbol"]] = q
    except Exception as e:
        raise PriceDataError(f"Could not fetch S&P 500 quotes: {e}") from e
    if not quotes:
        raise PriceDataError("Yahoo returned no S&P 500 quotes")
    _sp500_cache = (time.time(), (companies, quotes))
    return companies, quotes


def fetch_sp500_industry_returns():
    """Return one dict per Yahoo industry in the S&P 500 (sector, industry,
    avg_change_pct, weight_pct, count), largest first. avg_change_pct is the
    average of today's % change (or the last session's) across its stocks,
    weighted by _sp500_index_weight; weight_pct is its share of the index's
    total weight. Raises PriceDataError if the quotes can't be read."""
    companies, quotes = fetch_sp500_quotes()
    groups = {}
    for c in companies:
        q = quotes.get(c["symbol"]) or {}
        if q.get("marketCap") and q.get("regularMarketChangePercent") is not None:
            groups.setdefault((c["sector"], c["industry"]), []).append(
                (_sp500_index_weight(q), q["regularMarketChangePercent"]))
    if not groups:
        raise PriceDataError("Yahoo returned no usable S&P 500 quotes")

    total_cap = sum(cap for stocks in groups.values() for cap, _ in stocks)
    industries = [
        {
            "sector": sector,
            "industry": industry,
            "avg_change_pct": sum(cap * pct for cap, pct in stocks) / sum(cap for cap, _ in stocks),
            "weight_pct": sum(cap for cap, _ in stocks) / total_cap * 100,
            "count": len(stocks),
        }
        for (sector, industry), stocks in groups.items()
    ]
    industries.sort(key=lambda row: row["weight_pct"], reverse=True)
    return industries
