#!/usr/bin/env python3
"""
stock_price.py — fetch stock price history for the Dash app's price chart.

SEC EDGAR (used by company_growth_calc.py) has no price data, so this pulls
from Yahoo Finance via yfinance. Kept separate from company_growth_calc.py
since it's a different data source with its own failure modes (Yahoo
symbols can differ slightly from SEC tickers, e.g. share classes).
"""

import time
from datetime import date, timedelta

import yfinance as yf


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
    "1D": {"period": "1d", "interval": "5m"},
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


def fetch_price_history(ticker, range_key):
    """Return a DataFrame with a DatetimeIndex and 'Close'/'Volume' columns
    for `ticker` over `range_key` (one of RANGE_KEYS). Raises PriceDataError
    if Yahoo Finance has no data for this ticker/range."""
    config = _RANGE_CONFIG.get(range_key)
    if config is None:
        raise PriceDataError(f"Unknown range '{range_key}'.")

    kwargs = {"interval": config["interval"], "auto_adjust": True}
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
        df = yf.Ticker(ticker).history(**kwargs)
    except Exception as e:
        raise PriceDataError(f"Could not fetch price data for {ticker}: {e}") from e

    if df is None or df.empty or "Close" not in df.columns:
        raise PriceDataError(f"No price data available for {ticker} ({range_key}).")

    df = df[~df["Close"].isna()]
    if df.empty:
        raise PriceDataError(f"No price data available for {ticker} ({range_key}).")

    if "Volume" not in df.columns:
        df = df.assign(Volume=None)

    return df[["Close", "Volume"]]


def fetch_ticker_overview(ticker):
    """Return a dict of yfinance's own overview stats for `ticker` (market
    cap, trailing P/E, trailing-twelve-month revenue, net margin, exchange,
    sector, analyst price target and buy/hold/sell split) for the Company
    Tracker's KPI tiles. Individual fields come back None when yfinance
    doesn't have them for this ticker (e.g. ETFs have no P/E or analyst
    coverage) -- only raises if the ticker itself can't be resolved at all."""
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
        "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "target_mean_price": info.get("targetMeanPrice"),
        **_fetch_analyst_split(ticker_obj),
    }


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


def fetch_earnings_history(ticker, quarters=8):
    """Return a list of {"date", "estimate", "actual"} dicts, oldest first,
    for `ticker`'s last `quarters` reported quarters plus the next upcoming
    report (actual None) when Yahoo has one scheduled. "date" is the
    report's calendar date; EPS figures are Yahoo's adjusted (non-GAAP)
    numbers, the same basis analysts' consensus estimates use. Raises
    PriceDataError if Yahoo has no earnings history (funds, recent IPOs)."""
    try:
        # limit counts upcoming rows too, so ask for a few spare.
        df = yf.Ticker(ticker).get_earnings_dates(limit=quarters + 4)
    except Exception as e:
        raise PriceDataError(f"Could not fetch earnings history for {ticker}: {e}") from e
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
