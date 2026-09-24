#!/usr/bin/env python3
"""
stock_price.py — fetch stock price history for the Dash app's price chart.

SEC EDGAR (used by company_growth_calc.py) has no price data, so this pulls
from Yahoo Finance via yfinance. Kept separate from company_growth_calc.py
since it's a different data source with its own failure modes (Yahoo
symbols can differ slightly from SEC tickers, e.g. share classes).
"""

from datetime import date, timedelta

import yfinance as yf


class PriceDataError(Exception):
    """Raised when price history can't be fetched or is empty."""


class HoldingsDataError(Exception):
    """Raised when a fund's top-holdings breakdown can't be fetched or is empty."""


# range key -> yfinance kwargs. Intraday ranges use fine intervals (Yahoo
# only retains 1m data for ~7 days and other intraday data for ~60 days,
# which is why 1D/5D/1M each need a different granularity). "3Y" isn't a
# valid yfinance `period` string, so it's built from explicit start/end.
_RANGE_CONFIG = {
    "1D": {"period": "1d", "interval": "5m"},
    "5D": {"period": "5d", "interval": "15m"},
    "1M": {"period": "1mo", "interval": "1d"},
    "3M": {"period": "3mo", "interval": "1d"},
    "6M": {"period": "6mo", "interval": "1d"},
    "1Y": {"period": "1y", "interval": "1d"},
    "3Y": {"years_back": 3, "interval": "1d"},
    "ALL": {"period": "max", "interval": "1d"},
}

RANGE_KEYS = ["1D", "5D", "1M", "3M", "6M", "1Y", "3Y", "ALL"]


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
